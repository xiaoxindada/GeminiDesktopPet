"""额度快照解析（新/旧版回包）与无数据源时的失败兜底。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.antigravity_quota_service import (
    AntigravityQuotaService,
    _parse_quota_summary,
    _parse_user_status,
)


def test_parse_quota_summary_normalizes_groups_and_skips_unusable_buckets() -> None:
    body = {
        "response": {
            "description": "本周额度",
            "groups": [
                {
                    "displayName": "Gemini",
                    "buckets": [
                        {
                            "bucketId": "weekly",
                            "displayName": "Weekly Limit",
                            "remaining": {"remainingFraction": 0.42},
                            "resetTime": "2026-11-20T00:00:00Z",
                        },
                        {"bucketId": "5h", "remainingFraction": "0.9"},
                        {"displayName": "no-window", "remainingFraction": 0.5},
                    ],
                },
                {"displayName": "Claude and GPT", "buckets": [{"bucketId": "weekly", "remainingFraction": 1}]},
                {"displayName": "empty", "buckets": []},
            ],
        }
    }

    overview, groups = _parse_quota_summary(body)

    assert overview == "本周额度"
    assert [group.name for group in groups] == ["Gemini Models", "Claude and GPT models"]
    assert [bucket.window for bucket in groups[0].buckets] == ["weekly", "5h"]
    assert groups[0].buckets[0].remaining_fraction == pytest.approx(0.42)
    assert groups[0].buckets[0].reset_time == datetime(2026, 11, 20, tzinfo=timezone.utc)
    assert groups[0].buckets[1].remaining_fraction == pytest.approx(0.9)


def test_parse_legacy_user_status_reads_account_and_model_pools() -> None:
    body = {
        "userStatus": {"email": "pet@example.com", "planInfo": {"planDisplayName": "Google AI Pro"}},
        "clientModelConfigs": [
            {"modelOrAlias": {"model": "gemini-2.5-pro"}, "quotaInfo": {"remainingFraction": 0.35}},
            {"modelOrAlias": {"model": "gemini-2.5-flash"}, "quotaInfo": {"remainingFraction": 0.8}},
            {"modelOrAlias": {"model": "claude-sonnet-4"}, "quotaInfo": {"remainingFraction": 0.6}},
        ],
    }

    email, plan, groups = _parse_user_status(body)

    assert email == "pet@example.com"
    assert plan == "Google AI Pro"
    assert [group.name for group in groups] == ["Gemini Pro", "Gemini Flash", "Claude"]
    assert [group.buckets[0].remaining_fraction for group in groups] == [0.35, 0.8, 0.6]
    assert all(group.buckets[0].window == "weekly" for group in groups)


def test_fetch_quota_without_sources_reports_friendly_failure(monkeypatch) -> None:
    service = AntigravityQuotaService()
    monkeypatch.setattr(AntigravityQuotaService, "_discover_sources", staticmethod(lambda: []))

    snapshot = service.fetch_quota()

    assert snapshot.success is False
    assert snapshot.error_message is not None
    assert "未检测到 Antigravity" in snapshot.error_message
    assert snapshot.groups == []
