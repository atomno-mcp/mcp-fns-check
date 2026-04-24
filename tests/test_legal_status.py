"""Тесты get_legal_status: статус из ЕГРЮЛ + опциональное обогащение через ЕФРСБ."""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.errors import NotFoundError, ValidationError
from atomno_mcp_fns_check.sources.efrsb import EFRSB_BASE_URL, EFRSB_SEARCH_PATH, EfrsbClient
from atomno_mcp_fns_check.sources.egrul import EGRUL_BASE_URL, EgrulClient
from atomno_mcp_fns_check.tools.legal_status import get_legal_status

EGRUL_TOKEN = "tk-1"
EFRSB_URL = f"{EFRSB_BASE_URL}{EFRSB_SEARCH_PATH}"


def _mock_egrul_search(m: respx.MockRouter, payload: dict) -> None:
    m.post(f"{EGRUL_BASE_URL}/").mock(return_value=httpx.Response(200, json={"t": EGRUL_TOKEN}))
    m.get(f"{EGRUL_BASE_URL}/search-result/{EGRUL_TOKEN}").mock(
        return_value=httpx.Response(200, json=payload)
    )


def _sber_payload():
    return {
        "rows": [
            {
                "i": "7707083893",
                "o": "1027700132195",
                "n": "ПАО СБЕРБАНК",
                "k": "ul",
            }
        ]
    }


class TestGetLegalStatus:
    async def test_validation_neither_inn_nor_ogrn(self):
        async with EgrulClient(backoff_base=0.0) as e, EfrsbClient() as f:
            with pytest.raises(ValidationError):
                await get_legal_status(egrul=e, efrsb=f)

    async def test_invalid_inn(self):
        async with EgrulClient(backoff_base=0.0) as e, EfrsbClient() as f:
            with pytest.raises(ValidationError):
                await get_legal_status(inn="1111111111", egrul=e, efrsb=f)

    async def test_active_no_bankruptcy(self):
        with respx.mock() as m:
            _mock_egrul_search(m, _sber_payload())
            m.get(EFRSB_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))

            async with EgrulClient(backoff_base=0.0) as e, EfrsbClient() as f:
                report = await get_legal_status(inn="7707083893", egrul=e, efrsb=f)

        assert report.status == "active"
        assert report.status_label_ru == "Действующее"
        assert "egrul" in report.sources_checked
        assert "efrsb" in report.sources_checked
        assert report.bankruptcy_stage is None
        assert report.warnings == []

    async def test_bankruptcy_overrides_active(self):
        with respx.mock() as m:
            _mock_egrul_search(m, _sber_payload())
            m.get(EFRSB_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "pageData": [
                            {
                                "caseNumber": "А40-99/2025",
                                "stageName": "Наблюдение",
                                "isActive": True,
                            }
                        ]
                    },
                )
            )
            async with EgrulClient(backoff_base=0.0) as e, EfrsbClient() as f:
                report = await get_legal_status(inn="7707083893", egrul=e, efrsb=f)

        assert report.status == "bankruptcy"
        assert report.bankruptcy_stage == "observation"
        assert report.status_label_ru == "Банкротство"

    async def test_efrsb_failure_falls_back_to_egrul(self):
        with respx.mock() as m:
            _mock_egrul_search(m, _sber_payload())
            m.get(EFRSB_URL).mock(return_value=httpx.Response(503))

            async with EgrulClient(backoff_base=0.0) as e, EfrsbClient() as f:
                report = await get_legal_status(inn="7707083893", egrul=e, efrsb=f)

        assert report.status == "active"
        assert report.warnings
        assert "ЕФРСБ" in report.warnings[0]
        assert report.sources_checked == ["egrul"]

    async def test_liquidated_in_egrul_skips_efrsb(self):
        payload = {
            "rows": [
                {
                    "i": "7707083893",
                    "o": "1027700132195",
                    "n": "Ликвидированное ООО",
                    "dl": "2023-05-12",
                    "k": "ul",
                }
            ]
        }
        with respx.mock(assert_all_called=False) as m:
            _mock_egrul_search(m, payload)
            efrsb_route = m.get(EFRSB_URL).mock(
                return_value=httpx.Response(200, json={"pageData": []})
            )
            async with EgrulClient(backoff_base=0.0) as e, EfrsbClient() as f:
                report = await get_legal_status(inn="7707083893", egrul=e, efrsb=f)

        assert report.status == "liquidated"
        assert report.status_changed_at is not None
        assert efrsb_route.called is False
        assert report.sources_checked == ["egrul"]

    async def test_not_found(self):
        with respx.mock() as m:
            _mock_egrul_search(m, {"rows": []})

            async with EgrulClient(backoff_base=0.0) as e, EfrsbClient() as f:
                with pytest.raises(NotFoundError):
                    await get_legal_status(inn="7707083893", egrul=e, efrsb=f)

    async def test_efrsb_none_skipped_silently(self):
        with respx.mock() as m:
            _mock_egrul_search(m, _sber_payload())
            async with EgrulClient(backoff_base=0.0) as e:
                report = await get_legal_status(inn="7707083893", egrul=e, efrsb=None)
        assert report.status == "active"
        assert report.sources_checked == ["egrul"]
