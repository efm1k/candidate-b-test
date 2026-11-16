#!/usr/bin/env python3
"""Автотесты расчёта взаиморасчётов."""

from __future__ import annotations

import io
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from report import (
    Contract,
    Payment,
    Work,
    ZERO,
    build_report,
    format_report,
    load_data,
    main,
    money_invariants,
    parse_date,
    parse_money,
    settle_contract,
)


DATA_DIR = Path(__file__).resolve().parent / "data"
D = Decimal


def money(value: str) -> Decimal:
    return parse_money(value)


def contract(cid="D-X", amount="100000.00") -> Contract:
    return Contract(cid, "Подрядчик", money(amount), date(2026, 1, 1))


def work(
    wid,
    cid="D-X",
    price="10000.00",
    status="planned",
    completed=None,
    cancelled=None,
    title="Работа",
) -> Work:
    return Work(wid, cid, title, money(price), status, completed, cancelled)


def payment(
    pid,
    cid="D-X",
    amount="1000.00",
    status="paid",
    work_id=None,
    paid=None,
    refunded=None,
    created=None,
    purpose="платёж",
) -> Payment:
    return Payment(
        payment_id=pid,
        contract_id=cid,
        work_id=work_id,
        purpose=purpose,
        amount=money(amount),
        dump_status=status,
        created_at=created or paid,
        paid_at=paid,
        refunded_at=refunded,
    )


def settle(works, payments, on, amount="100000.00"):
    return settle_contract(contract(amount=amount), works, payments, on)


class MoneyParsingTests(unittest.TestCase):
    def test_kopecks_stay_exact(self):
        a = parse_money("10.01")
        b = parse_money("10.02")
        self.assertEqual(a + b, money("20.03"))
        self.assertNotIsInstance(a, float)

    def test_empty_amount_is_zero(self):
        self.assertEqual(parse_money(""), ZERO)
        self.assertEqual(parse_money("  "), ZERO)

    def test_date_formats(self):
        self.assertEqual(parse_date("10.11.2026"), date(2026, 11, 10))
        self.assertEqual(parse_date("2026-11-10"), date(2026, 11, 10))
        self.assertIsNone(parse_date(""))
        with self.assertRaises(ValueError):
            parse_date("11/10/2026")


class AdvanceAllocationTests(unittest.TestCase):
    def test_advance_covers_completed_works_in_acceptance_order(self):
        works = [
            work("W-1", price="100.00", status="done", completed=date(2026, 9, 1)),
            work("W-2", price="100.00", status="done", completed=date(2026, 9, 2)),
            work("W-3", price="100.00", status="planned"),
        ]
        payments = [
            payment("P-1", amount="150.00", paid=date(2026, 8, 1), purpose="аванс"),
        ]
        result = settle(works, payments, date(2026, 9, 10), amount="300.00")
        by_id = {item.work.work_id: item for item in result.works}
        self.assertEqual(by_id["W-1"].paid, money("100.00"))
        self.assertEqual(by_id["W-1"].remaining, ZERO)
        self.assertEqual(by_id["W-2"].paid, money("50.00"))
        self.assertEqual(by_id["W-2"].remaining, money("50.00"))
        self.assertEqual(by_id["W-3"].paid, ZERO)
        self.assertEqual(by_id["W-3"].remaining, money("100.00"))
        self.assertEqual(result.unallocated, ZERO)
        self.assertEqual(result.net_to_pay, money("150.00"))

    def test_leftover_advance_stays_unallocated_not_assigned_to_planned(self):
        works = [
            work("W-1", price="100.00", status="done", completed=date(2026, 9, 1)),
            work("W-2", price="100.00", status="planned"),
        ]
        payments = [
            payment("P-1", amount="150.00", paid=date(2026, 8, 1)),
        ]
        result = settle(works, payments, date(2026, 9, 10), amount="200.00")
        by_id = {item.work.work_id: item for item in result.works}
        self.assertEqual(by_id["W-1"].paid, money("100.00"))
        self.assertEqual(by_id["W-2"].paid, ZERO)
        self.assertEqual(by_id["W-2"].remaining, money("100.00"))
        self.assertEqual(result.unallocated, money("50.00"))
        self.assertEqual(result.net_to_pay, money("50.00"))


class DirectPaymentTests(unittest.TestCase):
    def test_direct_payment_on_planned_work_stays_there(self):
        works = [
            work("W-done", price="100.00", status="done", completed=date(2026, 9, 1)),
            work("W-plan", price="100.00", status="planned"),
        ]
        payments = [
            payment("P-dir", amount="15.00", work_id="W-plan", paid=date(2026, 8, 15)),
            payment("P-adv", amount="100.00", paid=date(2026, 8, 1)),
        ]
        result = settle(works, payments, date(2026, 9, 10), amount="200.00")
        by_id = {item.work.work_id: item for item in result.works}
        self.assertEqual(by_id["W-plan"].direct_paid, money("15.00"))
        self.assertEqual(by_id["W-plan"].advance_paid, ZERO)
        self.assertEqual(by_id["W-plan"].remaining, money("85.00"))
        self.assertEqual(by_id["W-done"].paid, money("100.00"))
        self.assertEqual(result.unallocated, ZERO)
        self.assertEqual(result.net_to_pay, money("85.00"))
        self.assertEqual(money_invariants(result), [])

    def test_unknown_work_id_joins_contract_pool(self):
        works = [
            work("W-1", price="100.00", status="done", completed=date(2026, 9, 1)),
        ]
        payments = [
            payment("P-x", amount="40.00", work_id="W-MISSING", paid=date(2026, 8, 1)),
        ]
        result = settle(works, payments, date(2026, 9, 10), amount="100.00")
        self.assertEqual(result.works[0].paid, money("40.00"))
        self.assertEqual(result.unallocated, ZERO)
        self.assertIn("не найден", result.payments[0].comment)
        self.assertEqual(money_invariants(result), [])

    def test_direct_payment_binds_to_work_before_advance(self):
        # W-2 принята раньше: сначала её прямой платёж, затем аванс добивает остаток.
        works = [
            work("W-1", price="100.00", status="done", completed=date(2026, 9, 2)),
            work("W-2", price="100.00", status="done", completed=date(2026, 9, 1)),
        ]
        payments = [
            payment("P-dir", amount="40.00", work_id="W-2", paid=date(2026, 8, 15)),
            payment("P-adv", amount="100.00", paid=date(2026, 8, 1)),
        ]
        result = settle(works, payments, date(2026, 9, 10), amount="200.00")
        by_id = {item.work.work_id: item for item in result.works}
        self.assertEqual(by_id["W-2"].direct_paid, money("40.00"))
        self.assertEqual(by_id["W-2"].advance_paid, money("60.00"))
        self.assertEqual(by_id["W-2"].paid, money("100.00"))
        self.assertEqual(by_id["W-1"].paid, money("40.00"))
        self.assertEqual(by_id["W-1"].advance_paid, money("40.00"))
        self.assertEqual(result.unallocated, ZERO)
        self.assertEqual(result.net_to_pay, money("60.00"))


class RefundTests(unittest.TestCase):
    def test_refunded_payment_is_zero_after_refund_date(self):
        works = [work("W-1", price="100.00", status="planned")]
        payments = [
            payment(
                "P-r",
                amount="50.00",
                status="refunded",
                paid=date(2026, 9, 2),
                refunded=date(2026, 9, 10),
            )
        ]
        after = settle(works, payments, date(2026, 9, 10), amount="100.00")
        self.assertEqual(after.paid_total, ZERO)
        self.assertEqual(after.unallocated, ZERO)

    def test_refunded_payment_counts_between_paid_and_refund(self):
        works = [work("W-1", price="100.00", status="planned")]
        payments = [
            payment(
                "P-r",
                amount="50.00",
                status="refunded",
                paid=date(2026, 9, 2),
                refunded=date(2026, 9, 10),
            )
        ]
        mid = settle(works, payments, date(2026, 9, 5), amount="100.00")
        self.assertEqual(mid.paid_total, money("50.00"))
        self.assertEqual(mid.unallocated, money("50.00"))


class QueuedPaymentTests(unittest.TestCase):
    def test_queued_payment_is_ignored_until_paid(self):
        works = [work("W-1", price="100.00", status="planned")]
        payments = [
            payment(
                "P-q",
                amount="80.00",
                status="queued",
                created=date(2026, 11, 1),
                paid=None,
            )
        ]
        result = settle(works, payments, date(2026, 11, 10), amount="100.00")
        self.assertEqual(result.paid_total, ZERO)
        self.assertEqual(result.queued_total, money("80.00"))
        self.assertEqual(result.net_to_pay, money("100.00"))

    def test_queued_payment_absent_before_it_is_created(self):
        works = [work("W-1", price="100.00", status="planned")]
        payments = [
            payment(
                "P-q",
                amount="80.00",
                status="queued",
                created=date(2026, 11, 1),
                paid=None,
            )
        ]
        result = settle(works, payments, date(2026, 10, 31), amount="100.00")
        self.assertEqual(result.queued_total, ZERO)
        self.assertEqual(result.payments[0].state, "not_created")


class CancelledWorkTests(unittest.TestCase):
    def test_cancelled_work_has_zero_obligation_and_does_not_eat_advance(self):
        works = [
            work("W-1", price="100.00", status="done", completed=date(2026, 9, 1)),
            work(
                "W-c",
                price="100.00",
                status="cancelled",
                cancelled=date(2026, 10, 12),
            ),
            work("W-3", price="100.00", status="planned"),
        ]
        payments = [payment("P-1", amount="150.00", paid=date(2026, 8, 1))]
        result = settle(works, payments, date(2026, 10, 20), amount="300.00")
        by_id = {item.work.work_id: item for item in result.works}
        self.assertEqual(by_id["W-c"].obligation, ZERO)
        self.assertEqual(by_id["W-c"].remaining, ZERO)
        self.assertEqual(by_id["W-c"].paid, ZERO)
        self.assertEqual(by_id["W-1"].paid, money("100.00"))
        self.assertEqual(result.unallocated, money("50.00"))
        self.assertEqual(result.cancelled_works_total, money("100.00"))
        self.assertEqual(result.active_works_total, money("200.00"))
        self.assertEqual(result.net_to_pay, money("50.00"))

    def test_work_still_active_before_cancellation_date(self):
        works = [
            work(
                "W-c",
                price="100.00",
                status="cancelled",
                cancelled=date(2026, 10, 12),
            )
        ]
        payments = [payment("P-1", amount="40.00", paid=date(2026, 8, 1))]
        before = settle(works, payments, date(2026, 10, 11), amount="100.00")
        after = settle(works, payments, date(2026, 10, 12), amount="100.00")
        self.assertEqual(before.works[0].status, "planned")
        self.assertEqual(before.works[0].obligation, money("100.00"))
        self.assertEqual(before.unallocated, money("40.00"))
        self.assertEqual(after.works[0].status, "cancelled")
        self.assertEqual(after.works[0].obligation, ZERO)
        self.assertEqual(after.unallocated, money("40.00"))
        self.assertEqual(after.net_to_pay, ZERO)
        self.assertEqual(after.overpayment, money("40.00"))

    def test_direct_payment_on_cancelled_work_is_excess_not_advance(self):
        works = [
            work("W-1", price="100.00", status="done", completed=date(2026, 9, 1)),
            work(
                "W-c",
                price="100.00",
                status="cancelled",
                cancelled=date(2026, 10, 12),
            ),
        ]
        payments = [
            payment("P-dir", amount="20.00", work_id="W-c", paid=date(2026, 9, 1)),
            payment("P-adv", amount="100.00", paid=date(2026, 8, 1)),
        ]
        result = settle(works, payments, date(2026, 10, 20), amount="200.00")
        by_id = {item.work.work_id: item for item in result.works}
        self.assertEqual(by_id["W-c"].paid, money("20.00"))
        self.assertEqual(by_id["W-c"].remaining, ZERO)
        self.assertEqual(by_id["W-c"].excess, money("20.00"))
        self.assertEqual(by_id["W-c"].advance_paid, ZERO)
        self.assertEqual(by_id["W-1"].paid, money("100.00"))
        self.assertEqual(result.unallocated, ZERO)
        self.assertEqual(result.net_to_pay, ZERO)
        self.assertEqual(result.overpayment, money("20.00"))
        self.assertEqual(money_invariants(result), [])


class HistoricalDateTests(unittest.TestCase):
    def test_dump_done_work_is_planned_until_completed_at(self):
        works = [
            work("W-late", price="40.00", status="done", completed=date(2026, 11, 11))
        ]
        payments = [payment("P-1", amount="25.00", paid=date(2026, 11, 2))]
        before = settle(works, payments, date(2026, 11, 10), amount="40.00")
        after = settle(works, payments, date(2026, 11, 11), amount="40.00")
        self.assertEqual(before.works[0].status, "planned")
        self.assertEqual(before.works[0].paid, ZERO)
        self.assertEqual(before.unallocated, money("25.00"))
        self.assertEqual(before.net_to_pay, money("15.00"))
        self.assertEqual(after.works[0].status, "done")
        self.assertEqual(after.works[0].paid, money("25.00"))
        self.assertEqual(after.works[0].remaining, money("15.00"))
        self.assertEqual(after.unallocated, ZERO)

    def test_future_payment_is_ignored(self):
        works = [work("W-1", price="100.00", status="done", completed=date(2026, 1, 1))]
        payments = [payment("P-1", amount="100.00", paid=date(2026, 11, 12))]
        result = settle(works, payments, date(2026, 11, 10), amount="100.00")
        self.assertEqual(result.paid_total, ZERO)
        self.assertEqual(result.net_to_pay, money("100.00"))

    def test_created_but_not_yet_paid_is_ignored(self):
        works = [work("W-1", price="100.00", status="done", completed=date(2026, 1, 1))]
        payments = [
            payment(
                "P-1",
                amount="100.00",
                status="paid",
                created=date(2026, 11, 1),
                paid=date(2026, 11, 12),
            )
        ]
        result = settle(works, payments, date(2026, 11, 10), amount="100.00")
        self.assertEqual(result.paid_total, ZERO)
        self.assertEqual(result.payments[0].state, "future")
        self.assertEqual(result.net_to_pay, money("100.00"))


class OverpaymentTests(unittest.TestCase):
    def test_unallocated_after_all_works_covered_is_overpayment(self):
        works = [
            work("W-1", price="20.00", status="done", completed=date(2026, 10, 1)),
            work("W-2", price="30.00", status="done", completed=date(2026, 10, 10)),
            work("W-3", price="40.00", status="done", completed=date(2026, 11, 11)),
        ]
        payments = [payment("P-1", amount="95.00", paid=date(2026, 11, 12))]
        result = settle(works, payments, date(2026, 11, 12), amount="90.00")
        self.assertEqual(result.paid_total, money("95.00"))
        self.assertEqual(result.allocated, money("90.00"))
        self.assertEqual(result.unallocated, money("5.00"))
        self.assertEqual(result.net_to_pay, ZERO)
        self.assertEqual(result.overpayment, money("5.00"))

    def test_direct_overpay_on_single_work(self):
        works = [work("W-1", price="100.00", status="done", completed=date(2026, 9, 1))]
        payments = [
            payment("P-1", amount="130.00", work_id="W-1", paid=date(2026, 9, 2))
        ]
        result = settle(works, payments, date(2026, 9, 10), amount="100.00")
        self.assertEqual(result.works[0].paid, money("130.00"))
        self.assertEqual(result.works[0].remaining, ZERO)
        self.assertEqual(result.works[0].excess, money("30.00"))
        self.assertEqual(result.overpayment, money("30.00"))
        self.assertEqual(result.unallocated, ZERO)


class InvariantTests(unittest.TestCase):
    def test_invariants_hold_on_synthetic_mix(self):
        works = [
            work("W-1", price="10.01", status="done", completed=date(2026, 9, 1)),
            work("W-2", price="20.02", status="planned"),
            work(
                "W-3",
                price="30.03",
                status="cancelled",
                cancelled=date(2026, 9, 5),
            ),
        ]
        payments = [
            payment("P-a", amount="15.15", paid=date(2026, 8, 1)),
            payment("P-d", amount="1.10", work_id="W-2", paid=date(2026, 8, 2)),
            payment(
                "P-r",
                amount="5.05",
                status="refunded",
                paid=date(2026, 8, 3),
                refunded=date(2026, 8, 10),
            ),
            payment("P-q", amount="9.99", status="queued", created=date(2026, 8, 4)),
        ]
        result = settle(works, payments, date(2026, 9, 20), amount="60.06")
        self.assertEqual(money_invariants(result), [])
        self.assertEqual(result.paid_total, money("16.25"))
        paid_on_works = sum((w.paid for w in result.works), ZERO)
        self.assertEqual(paid_on_works + result.unallocated, result.paid_total)
        self.assertEqual(
            result.paid_total + result.net_to_pay - result.overpayment,
            result.active_works_total,
        )


class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contracts, cls.works, cls.payments = load_data(DATA_DIR)

    def report_on(self, day: date):
        report = build_report(self.contracts, self.works, self.payments, day)
        problems = []
        for block in report.contracts:
            problems.extend(money_invariants(block))
        self.assertEqual(problems, [])
        return {block.contract.contract_id: block for block in report.contracts}

    def test_main_date_2026_11_10(self):
        by_c = self.report_on(date(2026, 11, 10))
        d1 = by_c["D-1"]
        d2 = by_c["D-2"]
        w = {item.work.work_id: item for item in d1.works}

        self.assertEqual(d1.paid_total, money("315000.00"))
        self.assertEqual(d1.allocated, money("300000.00"))
        self.assertEqual(d1.unallocated, money("15000.00"))
        self.assertEqual(d1.net_to_pay, money("185000.00"))
        self.assertEqual(d1.overpayment, ZERO)
        self.assertEqual(d1.queued_total, money("150000.00"))
        self.assertEqual(d1.active_works_total, money("500000.00"))
        self.assertEqual(d1.cancelled_works_total, money("100000.00"))
        self.assertEqual(d1.contract.amount - d1.active_works_total, money("100000.00"))

        self.assertEqual(w["W-11"].status, "done")
        self.assertEqual(w["W-11"].paid, money("100000.00"))
        self.assertEqual(w["W-12"].paid, money("100000.00"))
        self.assertEqual(w["W-13"].status, "cancelled")
        self.assertEqual(w["W-13"].remaining, ZERO)
        self.assertEqual(w["W-14"].direct_paid, money("15000.00"))
        self.assertEqual(w["W-14"].advance_paid, money("85000.00"))
        self.assertEqual(w["W-14"].paid, money("100000.00"))
        self.assertEqual(w["W-15"].paid, ZERO)
        self.assertEqual(w["W-15"].remaining, money("100000.00"))
        self.assertEqual(w["W-16"].remaining, money("100000.00"))

        w2 = {item.work.work_id: item for item in d2.works}
        self.assertEqual(d2.paid_total, money("75000.00"))
        self.assertEqual(d2.unallocated, money("25000.00"))
        self.assertEqual(d2.net_to_pay, money("15000.00"))
        self.assertEqual(w2["W-21"].paid, money("20000.00"))
        self.assertEqual(w2["W-22"].paid, money("30000.00"))
        self.assertEqual(w2["W-23"].status, "planned")
        self.assertEqual(w2["W-23"].paid, ZERO)
        self.assertEqual(w2["W-23"].remaining, money("40000.00"))

    def test_before_any_payment(self):
        by_c = self.report_on(date(2026, 8, 31))
        self.assertEqual(by_c["D-1"].paid_total, ZERO)
        self.assertEqual(by_c["D-1"].net_to_pay, money("600000.00"))

    def test_between_erroneous_pay_and_refund(self):
        by_c = self.report_on(date(2026, 9, 5))
        self.assertEqual(by_c["D-1"].paid_total, money("350000.00"))
        self.assertEqual(by_c["D-1"].unallocated, money("350000.00"))
        self.assertEqual(by_c["D-1"].queued_total, ZERO)

    def test_payment_does_not_exist_before_created_at(self):
        by_c = self.report_on(date(2026, 10, 31))
        self.assertEqual(by_c["D-1"].queued_total, ZERO)
        later = self.report_on(date(2026, 11, 1))
        self.assertEqual(later["D-1"].queued_total, money("150000.00"))
        self.assertEqual(later["D-1"].paid_total, money("315000.00"))

    def test_day_after_refund_before_first_acceptance(self):
        by_c = self.report_on(date(2026, 9, 15))
        self.assertEqual(by_c["D-1"].paid_total, money("300000.00"))
        self.assertEqual(by_c["D-1"].unallocated, money("300000.00"))
        self.assertEqual(by_c["D-1"].net_to_pay, money("300000.00"))

    def test_materials_payment_before_work_is_accepted(self):
        by_c = self.report_on(date(2026, 10, 26))
        d1 = by_c["D-1"]
        w14 = next(item for item in d1.works if item.work.work_id == "W-14")
        self.assertEqual(w14.status, "planned")
        self.assertEqual(w14.direct_paid, money("15000.00"))
        self.assertEqual(w14.advance_paid, ZERO)
        self.assertEqual(w14.remaining, money("85000.00"))
        self.assertEqual(d1.unallocated, money("100000.00"))
        self.assertEqual(d1.paid_total, money("315000.00"))
        self.assertEqual(d1.net_to_pay, money("185000.00"))

    def test_after_w23_accepted_before_p8(self):
        by_c = self.report_on(date(2026, 11, 11))
        d2 = by_c["D-2"]
        w23 = next(item for item in d2.works if item.work.work_id == "W-23")
        self.assertEqual(w23.status, "done")
        self.assertEqual(w23.paid, money("25000.00"))
        self.assertEqual(w23.remaining, money("15000.00"))
        self.assertEqual(d2.unallocated, ZERO)
        self.assertEqual(d2.net_to_pay, money("15000.00"))
        self.assertEqual(d2.paid_total, money("75000.00"))

    def test_after_p8_overpayment(self):
        by_c = self.report_on(date(2026, 11, 12))
        d2 = by_c["D-2"]
        self.assertEqual(d2.paid_total, money("95000.00"))
        self.assertEqual(d2.allocated, money("90000.00"))
        self.assertEqual(d2.unallocated, money("5000.00"))
        self.assertEqual(d2.net_to_pay, ZERO)
        self.assertEqual(d2.overpayment, money("5000.00"))

    def test_invariants_on_event_dates(self):
        days = [
            date(2026, 8, 31),
            date(2026, 9, 1),
            date(2026, 9, 2),
            date(2026, 9, 10),
            date(2026, 9, 20),
            date(2026, 10, 1),
            date(2026, 10, 5),
            date(2026, 10, 12),
            date(2026, 10, 25),
            date(2026, 10, 28),
            date(2026, 10, 30),
            date(2026, 11, 2),
            date(2026, 11, 10),
            date(2026, 11, 11),
            date(2026, 11, 12),
        ]
        for day in days:
            by_c = self.report_on(day)
            for block in by_c.values():
                paid_on_works = sum((w.paid for w in block.works), ZERO)
                self.assertEqual(
                    paid_on_works + block.unallocated,
                    block.paid_total,
                    msg=f"{day} {block.contract.contract_id}",
                )

    def test_cli_runs_for_control_date(self):
        buf = io.StringIO()
        import sys

        old = sys.stdout
        sys.stdout = buf
        try:
            code = main(["10.11.2026", "--data-dir", str(DATA_DIR)])
        finally:
            sys.stdout = old
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn("10.11.2026", text)
        self.assertIn("D-1", text)
        self.assertIn("D-2", text)
        self.assertIn("315 000,00 ₽", text)
        self.assertIn("185 000,00 ₽", text)


class ReportTextTests(unittest.TestCase):
    def test_format_contains_unallocated_and_queued(self):
        contracts, works, payments = load_data(DATA_DIR)
        report = build_report(contracts, works, payments, date(2026, 11, 10))
        text = format_report(report)
        self.assertIn("Нераспределённый остаток", text)
        self.assertIn("В очереди, не учтено", text)
        self.assertIn("W-23", text)
        self.assertIn("ещё не принята", text)

    def test_overpayment_note_warns_not_to_sum_twice(self):
        contracts, works, payments = load_data(DATA_DIR)
        report = build_report(contracts, works, payments, date(2026, 11, 12))
        text = format_report(report)
        self.assertIn("не суммировать", text)
        self.assertIn("5 000,00 ₽", text)


if __name__ == "__main__":
    unittest.main()
