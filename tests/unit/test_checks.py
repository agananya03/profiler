"""Unit tests for checks.py integrity check assertions."""

from datetime import datetime, timezone
from profiler.checks import run_integrity_checks
from profiler.models import Fact, Profile


def test_run_integrity_checks_valid():
    now = datetime.now(timezone.utc)
    profile = Profile(
        orgnr="923609016",
        facts=[
            Fact(
                orgnr="923609016",
                field="financials",
                value={
                    "year": 2023,
                    "revenue": 1000000.0,
                    "operating_result": 200000.0,
                    "total_assets": 5000000.0,
                    "equity": 3000000.0,
                    "total_debt": 2000000.0,  # 3M + 2M = 5M (0% diff)
                },
                source_name="Regnskapsregisteret",
                source_url="https://api.example.com",
                retrieved_at=now,
                method="api",
                match_basis=["orgnr_exact"],
            )
        ],
    )

    warnings = run_integrity_checks(profile)
    assert len(warnings) == 0


def test_checks_negative_revenue():
    now = datetime.now(timezone.utc)
    profile = Profile(
        orgnr="923609016",
        facts=[
            Fact(
                orgnr="923609016",
                field="financials",
                value={
                    "year": 2023,
                    "revenue": -5000.0,
                },
                source_name="Regnskapsregisteret",
                source_url="https://api.example.com",
                retrieved_at=now,
                method="api",
                match_basis=["orgnr_exact"],
            )
        ],
    )

    warnings = run_integrity_checks(profile)
    assert any("Negative revenue" in w for w in warnings)


def test_checks_balance_sheet_equation_mismatch():
    now = datetime.now(timezone.utc)
    profile = Profile(
        orgnr="923609016",
        facts=[
            Fact(
                orgnr="923609016",
                field="financials",
                value={
                    "year": 2023,
                    "revenue": 1000000.0,
                    "total_assets": 10000000.0,
                    "equity": 2000000.0,
                    "total_debt": 2000000.0,  # 2M + 2M = 4M != 10M (60% diff > 2%)
                },
                source_name="Regnskapsregisteret",
                source_url="https://api.example.com",
                retrieved_at=now,
                method="api",
                match_basis=["orgnr_exact"],
            )
        ],
    )

    warnings = run_integrity_checks(profile)
    assert any("Balance sheet equation mismatch" in w for w in warnings)


def test_checks_flag_yoy_change_over_100x():
    now = datetime.now(timezone.utc)
    profile = Profile(
        orgnr="923609016",
        facts=[
            Fact(
                orgnr="923609016",
                field="financials",
                value={"year": 2022, "revenue": 10000.0},
                source_name="Regnskapsregisteret",
                source_url="https://api.example.com",
                retrieved_at=now,
                method="api",
                match_basis=["orgnr_exact"],
            ),
            Fact(
                orgnr="923609016",
                field="financials",
                value={"year": 2023, "revenue": 2000000.0},  # 200x increase (> 100x)
                source_name="Regnskapsregisteret",
                source_url="https://api.example.com",
                retrieved_at=now,
                method="api",
                match_basis=["orgnr_exact"],
            ),
        ],
    )

    warnings = run_integrity_checks(profile)
    assert any("year-on-year change over 100x" in w for w in warnings)
