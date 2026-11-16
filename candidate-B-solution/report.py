#!/usr/bin/env python3
"""Отчёт по взаиморасчётам с подрядчиками на заданную дату.

Деньги считаются в decimal.Decimal с точностью до копейки, без float.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional, Sequence


TWOPLACES = Decimal("0.01")
ZERO = Decimal("0.00")


def parse_money(value: str) -> Decimal:
    text = (value or "").strip()
    if not text:
        return ZERO
    return Decimal(text).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def parse_date(value: str) -> Optional[date]:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(
        f"Не удалось разобрать дату {value!r}. "
        "Ожидается ДД.ММ.ГГГГ или ГГГГ-ММ-ДД."
    )


def format_date(value: Optional[date]) -> str:
    if value is None:
        return "—"
    return value.strftime("%d.%m.%Y")


def format_money(amount: Decimal) -> str:
    quantized = amount.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    quantized = abs(quantized)
    whole, frac = f"{quantized:.2f}".split(".")
    groups = []
    while whole:
        groups.append(whole[-3:])
        whole = whole[:-3]
    grouped = " ".join(reversed(groups)) if groups else "0"
    return f"{sign}{grouped},{frac} ₽"


@dataclass
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: Optional[date]


@dataclass
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    dump_status: str
    completed_at: Optional[date]
    cancelled_at: Optional[date]


@dataclass
class Payment:
    payment_id: str
    contract_id: str
    work_id: Optional[str]
    purpose: str
    amount: Decimal
    dump_status: str
    created_at: Optional[date]
    paid_at: Optional[date]
    refunded_at: Optional[date]


@dataclass
class PaymentView:
    payment: Payment
    counted_amount: Decimal
    state: str
    comment: str


@dataclass
class WorkSettlement:
    work: Work
    status: str
    status_label: str
    obligation: Decimal
    direct_paid: Decimal
    advance_paid: Decimal
    paid: Decimal
    remaining: Decimal
    excess: Decimal


@dataclass
class ContractSettlement:
    contract: Contract
    works: list[WorkSettlement]
    payments: list[PaymentView]
    paid_total: Decimal
    allocated: Decimal
    unallocated: Decimal
    remaining_works: Decimal
    net_to_pay: Decimal
    overpayment: Decimal
    active_works_total: Decimal
    cancelled_works_total: Decimal
    queued_total: Decimal


@dataclass
class Report:
    report_date: date
    contracts: list[ContractSettlement]
    paid_total: Decimal = ZERO
    net_to_pay: Decimal = ZERO
    overpayment: Decimal = ZERO
    unallocated: Decimal = ZERO


STATUS_LABELS = {
    "done": "принята",
    "planned": "запланирована, ещё не принята",
    "cancelled": "отменена",
}


def effective_work_status(work: Work, report_date: date) -> str:
    """Статус работы на дату отчёта. Даты событий важнее статуса выгрузки."""
    if work.cancelled_at is not None and work.cancelled_at <= report_date:
        return "cancelled"
    if work.completed_at is not None and work.completed_at <= report_date:
        return "done"
    return "planned"


def payment_effect(payment: Payment, report_date: date) -> PaymentView:
    """Сколько из платежа реально «в деле» на дату отчёта."""
    if payment.created_at is not None and payment.created_at > report_date:
        return PaymentView(
            payment=payment,
            counted_amount=ZERO,
            state="not_created",
            comment=(
                f"заведён {format_date(payment.created_at)}, "
                "после даты отчёта — не учтён"
            ),
        )
    if payment.refunded_at is not None and payment.refunded_at <= report_date:
        return PaymentView(
            payment=payment,
            counted_amount=ZERO,
            state="refunded",
            comment=f"возвращён {format_date(payment.refunded_at)}, в нетто 0",
        )
    if payment.paid_at is not None and payment.paid_at <= report_date:
        return PaymentView(
            payment=payment,
            counted_amount=payment.amount,
            state="paid",
            comment="учтён",
        )
    if (payment.dump_status or "").strip() == "queued" or payment.paid_at is None:
        created = format_date(payment.created_at)
        return PaymentView(
            payment=payment,
            counted_amount=ZERO,
            state="queued",
            comment=f"в очереди с {created}, ещё не проведён — не учтён",
        )
    return PaymentView(
        payment=payment,
        counted_amount=ZERO,
        state="future",
        comment=f"оплачен {format_date(payment.paid_at)}, после даты отчёта — не учтён",
    )


def _allocate_pool(
    pool: Decimal,
    works: Sequence[WorkSettlement],
) -> Decimal:
    """Водопад: закрываем остаток обязательства, пока хватает пула."""
    for item in works:
        if pool <= ZERO:
            break
        need = item.obligation - item.paid
        if need <= ZERO:
            continue
        take = need if need <= pool else pool
        item.advance_paid += take
        item.paid += take
        pool -= take
    return pool


def settle_contract(
    contract: Contract,
    works: Sequence[Work],
    payments: Sequence[Payment],
    report_date: date,
) -> ContractSettlement:
    views = [payment_effect(p, report_date) for p in payments]
    paid_total = sum((v.counted_amount for v in views), ZERO)
    queued_total = sum(
        (v.payment.amount for v in views if v.state == "queued"),
        ZERO,
    )

    settlements: list[WorkSettlement] = []
    by_id: dict[str, WorkSettlement] = {}
    for work in works:
        status = effective_work_status(work, report_date)
        obligation = ZERO if status == "cancelled" else work.price
        item = WorkSettlement(
            work=work,
            status=status,
            status_label=STATUS_LABELS[status],
            obligation=obligation,
            direct_paid=ZERO,
            advance_paid=ZERO,
            paid=ZERO,
            remaining=ZERO,
            excess=ZERO,
        )
        settlements.append(item)
        by_id[work.work_id] = item

    pool = ZERO
    for view in views:
        if view.counted_amount == ZERO:
            continue
        work_id = view.payment.work_id
        if work_id:
            target = by_id.get(work_id)
            if target is None:
                # Нет такой работы в договоре: сумма идёт в общий пул аванса,
                # как платёж без work_id. Не теряем деньги и не падаем.
                view.comment += (
                    f"; work_id {work_id} не найден, сумма в пуле аванса договора"
                )
                pool += view.counted_amount
            else:
                target.direct_paid += view.counted_amount
                target.paid += view.counted_amount
        else:
            pool += view.counted_amount

    completed = sorted(
        (item for item in settlements if item.status == "done"),
        key=lambda item: (
            item.work.completed_at or date.min,
            item.work.work_id,
        ),
    )
    pool = _allocate_pool(pool, completed)

    for item in settlements:
        item.remaining = max(ZERO, item.obligation - item.paid)
        item.excess = max(ZERO, item.paid - item.obligation)

    allocated = sum((item.paid for item in settlements), ZERO)
    unallocated = paid_total - allocated
    remaining_works = sum((item.remaining for item in settlements), ZERO)
    net_raw = remaining_works - unallocated
    net_to_pay = net_raw if net_raw > ZERO else ZERO
    overpayment = -net_raw if net_raw < ZERO else ZERO
    # Переплата по отдельным работам (в т.ч. оплата отменённых) тоже excess.
    work_excess = sum((item.excess for item in settlements), ZERO)
    overpayment += work_excess

    active_total = sum((item.obligation for item in settlements), ZERO)
    cancelled_total = sum(
        (item.work.price for item in settlements if item.status == "cancelled"),
        ZERO,
    )

    return ContractSettlement(
        contract=contract,
        works=settlements,
        payments=views,
        paid_total=paid_total.quantize(TWOPLACES),
        allocated=allocated.quantize(TWOPLACES),
        unallocated=unallocated.quantize(TWOPLACES),
        remaining_works=remaining_works.quantize(TWOPLACES),
        net_to_pay=net_to_pay.quantize(TWOPLACES),
        overpayment=overpayment.quantize(TWOPLACES),
        active_works_total=active_total.quantize(TWOPLACES),
        cancelled_works_total=cancelled_total.quantize(TWOPLACES),
        queued_total=queued_total.quantize(TWOPLACES),
    )


def build_report(
    contracts: Sequence[Contract],
    works: Sequence[Work],
    payments: Sequence[Payment],
    report_date: date,
) -> Report:
    works_by_contract: dict[str, list[Work]] = {}
    for work in works:
        works_by_contract.setdefault(work.contract_id, []).append(work)
    payments_by_contract: dict[str, list[Payment]] = {}
    for payment in payments:
        payments_by_contract.setdefault(payment.contract_id, []).append(payment)

    settlements = [
        settle_contract(
            contract,
            works_by_contract.get(contract.contract_id, []),
            payments_by_contract.get(contract.contract_id, []),
            report_date,
        )
        for contract in contracts
    ]
    return Report(
        report_date=report_date,
        contracts=settlements,
        paid_total=sum((c.paid_total for c in settlements), ZERO),
        net_to_pay=sum((c.net_to_pay for c in settlements), ZERO),
        overpayment=sum((c.overpayment for c in settlements), ZERO),
        unallocated=sum((c.unallocated for c in settlements), ZERO),
    )


def load_contracts(path: Path) -> list[Contract]:
    rows: list[Contract] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            rows.append(
                Contract(
                    contract_id=raw["contract_id"].strip(),
                    contractor=raw["contractor"].strip(),
                    amount=parse_money(raw["amount"]),
                    signed_at=parse_date(raw["signed_at"]),
                )
            )
    return rows


def load_works(path: Path) -> list[Work]:
    rows: list[Work] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            work_id = raw["work_id"].strip()
            rows.append(
                Work(
                    work_id=work_id,
                    contract_id=raw["contract_id"].strip(),
                    title=raw["title"].strip(),
                    price=parse_money(raw["price"]),
                    dump_status=raw["status"].strip(),
                    completed_at=parse_date(raw["completed_at"]),
                    cancelled_at=parse_date(raw["cancelled_at"]),
                )
            )
    return rows


def load_payments(path: Path) -> list[Payment]:
    rows: list[Payment] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            work_raw = (raw.get("work_id") or "").strip()
            rows.append(
                Payment(
                    payment_id=raw["payment_id"].strip(),
                    contract_id=raw["contract_id"].strip(),
                    work_id=work_raw or None,
                    purpose=raw["purpose"].strip(),
                    amount=parse_money(raw["amount"]),
                    dump_status=raw["status"].strip(),
                    created_at=parse_date(raw["created_at"]),
                    paid_at=parse_date(raw["paid_at"]),
                    refunded_at=parse_date(raw["refunded_at"]),
                )
            )
    return rows


def load_data(data_dir: Path) -> tuple[list[Contract], list[Work], list[Payment]]:
    return (
        load_contracts(data_dir / "contracts.csv"),
        load_works(data_dir / "works.csv"),
        load_payments(data_dir / "payments.csv"),
    )


def money_invariants(settlement: ContractSettlement) -> list[str]:
    """Проверки, что деньги не потерялись и не посчитались дважды."""
    errors: list[str] = []
    cid = settlement.contract.contract_id
    paid_on_works = sum((w.paid for w in settlement.works), ZERO)
    if paid_on_works + settlement.unallocated != settlement.paid_total:
        errors.append(
            f"{cid}: выплачено по работам {paid_on_works} + нераспределено "
            f"{settlement.unallocated} != итог {settlement.paid_total}"
        )
    for work in settlement.works:
        if work.direct_paid + work.advance_paid != work.paid:
            errors.append(
                f"{cid}/{work.work.work_id}: прямо {work.direct_paid} + "
                f"аванс {work.advance_paid} != выплачено {work.paid}"
            )
        if work.obligation + work.excess != work.paid + work.remaining:
            errors.append(
                f"{cid}/{work.work.work_id}: обязательство {work.obligation} + "
                f"переплата {work.excess} != выплачено {work.paid} + "
                f"остаток {work.remaining}"
            )
        if work.advance_paid < ZERO or work.direct_paid < ZERO:
            errors.append(f"{cid}/{work.work.work_id}: отрицательная выплата")
    if settlement.unallocated < ZERO:
        errors.append(f"{cid}: отрицательный нераспределённый остаток")
    obligation = sum((w.obligation for w in settlement.works), ZERO)
    excess = sum((w.excess for w in settlement.works), ZERO)
    remaining = sum((w.remaining for w in settlement.works), ZERO)
    if obligation + excess != paid_on_works + remaining:
        errors.append(f"{cid}: инвариант обязательства/выплат нарушен")
    cash = settlement.paid_total + settlement.net_to_pay - settlement.overpayment
    if cash != settlement.active_works_total:
        errors.append(
            f"{cid}: выплачено {settlement.paid_total} + к оплате "
            f"{settlement.net_to_pay} − переплата {settlement.overpayment} "
            f"!= действующие работы {settlement.active_works_total}"
        )
    return errors


def format_report(report: Report) -> str:
    lines: list[str] = []
    add = lines.append
    width = 78
    add("=" * width)
    add(f"ОТЧЁТ ПО ВЗАИМОРАСЧЁТАМ НА {format_date(report.report_date)}")
    add("=" * width)
    add("Деньги — в рублях с копейками. Учтены только фактически ушедшие")
    add("платежи на дату отчёта. Аванс без work_id закрывает принятые работы")
    add("по очереди приёмки; нераспределённый остаток показан отдельно.")
    add("")

    for block in report.contracts:
        c = block.contract
        add("-" * width)
        add(f"Договор {c.contract_id} — {c.contractor}")
        add(f"Подписан: {format_date(c.signed_at)}")
        add(f"Сумма договора:              {format_money(c.amount)}")
        add(f"Действующие работы:          {format_money(block.active_works_total)}")
        add(f"Отменённые работы:           {format_money(block.cancelled_works_total)}")
        diff = c.amount - block.active_works_total
        add(f"Договор минус действующие:   {format_money(diff)}")
        add("")
        add("Работы:")
        for item in block.works:
            w = item.work
            extra = ""
            if item.status == "done" and w.completed_at:
                extra = f" ({format_date(w.completed_at)})"
            elif item.status == "cancelled" and w.cancelled_at:
                extra = f" ({format_date(w.cancelled_at)})"
            add(f"  {w.work_id}  {w.title}")
            add(f"    статус: {item.status_label}{extra}")
            add(
                f"    цена {format_money(w.price)}  |  "
                f"обязательство {format_money(item.obligation)}  |  "
                f"выплачено {format_money(item.paid)}  |  "
                f"остаток {format_money(item.remaining)}"
            )
            add(
                f"    из них напрямую {format_money(item.direct_paid)}, "
                f"из аванса договора {format_money(item.advance_paid)}"
            )
            if item.excess > ZERO:
                add(f"    переплата по работе: {format_money(item.excess)}")
        add("")
        add("Платежи:")
        if not block.payments:
            add("  нет платежей")
        for view in block.payments:
            p = view.payment
            bound = p.work_id if p.work_id else "без работы"
            add(
                f"  {p.payment_id}  {format_money(p.amount)}  "
                f"{bound}  {p.purpose}"
            )
            add(
                f"    создан {format_date(p.created_at)}, "
                f"оплачен {format_date(p.paid_at)}  →  {view.comment}"
            )
        add("")
        add(f"Фактически выплачено:         {format_money(block.paid_total)}")
        add(f"Распределено по работам:      {format_money(block.allocated)}")
        add(f"Нераспределённый остаток:     {format_money(block.unallocated)}")
        add(f"Остаток по колонкам работ:    {format_money(block.remaining_works)}")
        add(f"К оплате по договору (нетто): {format_money(block.net_to_pay)}")
        add(f"Переплата:                    {format_money(block.overpayment)}")
        if (
            block.overpayment > ZERO
            and block.unallocated > ZERO
            and block.remaining_works == ZERO
        ):
            add(
                "  (нераспределённый остаток и переплата — одни и те же деньги, "
                "не суммировать)"
            )
        if block.queued_total > ZERO:
            add(
                f"В очереди, не учтено:         {format_money(block.queued_total)}"
            )
        add("")

    add("=" * width)
    add("ИТОГО ПО ВСЕМ ДОГОВОРАМ")
    add(f"Выплачено:                   {format_money(report.paid_total)}")
    add(f"Нераспределённый остаток:    {format_money(report.unallocated)}")
    add(f"К оплате (нетто):            {format_money(report.net_to_pay)}")
    add(f"Переплата:                   {format_money(report.overpayment)}")
    add("=" * width)
    return "\n".join(lines) + "\n"


def default_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Отчёт по выплатам подрядчикам на дату.",
    )
    parser.add_argument(
        "date",
        help="Дата отчёта: 10.11.2026 или 2026-11-10",
    )
    parser.add_argument(
        "--data-dir",
        default=str(default_data_dir()),
        help="Папка с CSV (по умолчанию data/ рядом со скриптом)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report_date = parse_date(args.date)
        if report_date is None:
            raise ValueError("Дата отчёта не должна быть пустой")
        data_dir = Path(args.data_dir)
        contracts, works, payments = load_data(data_dir)
        report = build_report(contracts, works, payments, report_date)
        problems: list[str] = []
        for block in report.contracts:
            problems.extend(money_invariants(block))
        text = format_report(report)
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass
        sys.stdout.write(text)
        if problems:
            sys.stderr.write("Нарушены инварианты:\n")
            for problem in problems:
                sys.stderr.write(f"  {problem}\n")
            return 2
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI показывает ошибку пользователю
        sys.stderr.write(f"Ошибка: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
