"""Тесты check_individual_bankruptcy tool."""

from __future__ import annotations

import httpx
import pytest
import respx

from atomno_mcp_fns_check.errors import SourceUnavailableError, ValidationError
from atomno_mcp_fns_check.sources.efrsb import (
    EFRSB_BANKRUPTS_HTML_PATH,
    EFRSB_BANKRUPTS_PATH,
    EFRSB_BASE_URL,
    EfrsbClient,
)
from atomno_mcp_fns_check.tools.individual_bankruptcy import check_individual_bankruptcy

JSON_URL = f"{EFRSB_BASE_URL}{EFRSB_BANKRUPTS_PATH}"
HTML_URL = f"{EFRSB_BASE_URL}{EFRSB_BANKRUPTS_HTML_PATH}"
FIO = "Иванов Иван Иванович"
BIRTH = "1990-01-15"


class TestCheckIndividualBankruptcy:
    async def test_validation_short_fio(self):
        async with EfrsbClient() as efrsb:
            with pytest.raises(ValidationError):
                await check_individual_bankruptcy(
                    fio="X",
                    birth_date=BIRTH,
                    efrsb=efrsb,
                )

    async def test_no_active_cases(self):
        with respx.mock() as m:
            m.get(JSON_URL).mock(return_value=httpx.Response(200, json={"pageData": []}))
            m.get(HTML_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
            async with EfrsbClient() as efrsb:
                report = await check_individual_bankruptcy(
                    fio=FIO,
                    birth_date=BIRTH,
                    efrsb=efrsb,
                )
        assert report.has_active_bankruptcy is False
        assert report.active_cases == []
        assert report.query_fio == FIO
        dumped = report.model_dump()
        keys = list(dumped)
        assert keys[:4] == [
            "query_fio",
            "query_birth_date",
            "query_snils",
            "query_hash",
        ]
        assert dumped["source_registry_ru"].startswith("Единый федеральный реестр")
        assert dumped["source_url"] == "https://bankrot.fedresurs.ru"
        assert dumped["data_as_of"] is None
        assert "не публикует" in dumped["data_as_of_note_ru"]
        assert dumped["homonym_warning_ru"] is None
        assert "Запрошено в реестре" in dumped["notice_ru"]
        assert "UTC" in dumped["notice_ru"]
        assert "не публикует" in dumped["notice_ru"]
        assert "однофамилец" not in dumped["notice_ru"]
        assert "однофамилец" not in dumped["disclaimer_ru"]

    async def test_active_case_found(self):
        payload = {
            "pageData": [
                {
                    "caseNumber": "А41-100/2024",
                    "stageName": "Реструктуризация долгов",
                    "isActive": True,
                    "lastName": "Иванов",
                    "firstName": "Иван",
                    "middleName": "Иванович",
                    "birthDate": "1990-01-15",
                }
            ]
        }
        with respx.mock() as m:
            m.get(JSON_URL).mock(return_value=httpx.Response(200, json=payload))
            async with EfrsbClient() as efrsb:
                report = await check_individual_bankruptcy(
                    fio=FIO,
                    birth_date=BIRTH,
                    efrsb=efrsb,
                )
        assert report.has_active_bankruptcy is True
        assert len(report.active_cases) == 1
        assert report.active_cases[0].case_number == "А41-100/2024"
        assert report.active_cases[0].stage == "financial_recovery"
        assert report.active_cases[0].match_confidence == "exact"
        assert report.homonym_warning_ru is not None
        assert "однофамилец" in report.homonym_warning_ru
        assert "однофамилец" in report.notice_ru
        assert "однофамилец" in report.disclaimer_ru
        assert report.data_as_of is None
        assert "не публикует" in report.data_as_of_note_ru
        assert "Запрошено в реестре" in report.notice_ru
        assert "UTC" in report.notice_ru

    async def test_403_raises_source_unavailable(self):
        with respx.mock() as m:
            m.get(JSON_URL).mock(return_value=httpx.Response(403, text="blocked"))
            async with EfrsbClient() as efrsb:
                with pytest.raises(SourceUnavailableError) as exc:
                    await check_individual_bankruptcy(
                        fio=FIO,
                        birth_date=BIRTH,
                        efrsb=efrsb,
                    )
        assert exc.value.details.get("status") == 403

    async def test_html_fallback_used(self):
        html = '<script>{"pageData":[{"caseNumber":"А41-200/2024","stageName":"Наблюдение","isActive":true}]}</script>'
        with respx.mock() as m:
            m.get(JSON_URL).mock(return_value=httpx.Response(200, content=b"<html/>"))
            m.get(HTML_URL).mock(return_value=httpx.Response(200, text=html))
            async with EfrsbClient() as efrsb:
                report = await check_individual_bankruptcy(
                    fio=FIO,
                    birth_date=BIRTH,
                    efrsb=efrsb,
                )
        assert report.source == "efrsb_html"
        assert report.has_active_bankruptcy is True

    async def test_snils_disambiguation(self):
        payload = {
            "pageData": [
                {
                    "caseNumber": "А41-300/2024",
                    "stageName": "Наблюдение",
                    "isActive": True,
                    "birthDate": "1990-01-15",
                    "snils": "12345678901",
                },
                {
                    "caseNumber": "А41-301/2024",
                    "stageName": "Наблюдение",
                    "isActive": True,
                    "birthDate": "1990-01-15",
                    "snils": "10987654321",
                },
            ]
        }
        with respx.mock() as m:
            m.get(JSON_URL).mock(return_value=httpx.Response(200, json=payload))
            async with EfrsbClient() as efrsb:
                report = await check_individual_bankruptcy(
                    fio=FIO,
                    birth_date=BIRTH,
                    snils="123-456-789 01",
                    efrsb=efrsb,
                )
        assert len(report.active_cases) == 1
        assert report.active_cases[0].case_number == "А41-300/2024"
