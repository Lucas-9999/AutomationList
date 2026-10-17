"""Customer input integration; business facts and requests are synthetic."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradeflow.api import create_app
from tradeflow.agent import AgentClient, validate_inquiry_source




@pytest.mark.parametrize("cell", [{"formula": "1+1"}, {"formula_attributes": {"t": "shared"}}, {"data_type": "e"}])
def test_unverified_workbook_values_never_reach_agent(cell, monkeypatch):
    for key, value in (("BASE_URL", "https://synthetic.example.invalid/v1"), ("API_KEY", "SYNTHETIC"), ("MODEL", "synthetic")):
        monkeypatch.setenv("TRADEFLOW_AGENT_" + key, value)
    source = {"body": "SYNTHETIC cached 2", "source_type": "inquiry_workbook", "source": {"text_analysis_available": True}, "cells": [cell]}
    calls = []
    with pytest.raises(ValueError, match="未核验"):
        AgentClient(transport=lambda *args: calls.append(args)).analyze_inquiry(source)
    assert not calls


def test_image_only_workbook_cannot_be_analyzed():
    with pytest.raises(ValueError, match="没有可分析"):
        validate_inquiry_source({"source_type": "inquiry_workbook", "source": {"text_analysis_available": False}})


def test_api_source_rejection_is_audited_before_provider_configuration(tmp_path):
    app = create_app(tmp_path)
    inquiry = app.state.store.create_record("inquiry", "SYNTHETIC", {"body": "SYNTHETIC", "source_type": "inquiry_workbook", "source": {"text_analysis_available": True}, "cells": [{"formula": "1+1"}]})
    with TestClient(app) as client:
        result = client.post("/inquiries/" + inquiry["id"] + "/analyze")
        assert result.status_code == 422
        assert "未核验" in result.json()["detail"]
        assert client.get("/records?kind=analysis").json() == []
        assert client.get("/audit/" + inquiry["id"]).json()[-1]["detail"]["reason"] == "unverified_inquiry_source"
