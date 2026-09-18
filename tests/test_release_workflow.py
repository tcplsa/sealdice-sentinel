from pathlib import Path

import yaml


def test_release_workflow_supports_manual_repair_and_idempotent_upload() -> None:
    workflow_path = Path(".github/workflows/release.yml")
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)

    # PyYAML 1.1 treats the unquoted GitHub Actions key `on` as boolean true.
    triggers = workflow.get("on", workflow.get(True))
    assert "workflow_dispatch" in triggers
    assert triggers["workflow_dispatch"]["inputs"]["tag"]["required"] is True
    assert 'gh release view "${RELEASE_TAG}"' in workflow_text
    assert 'gh release upload "${RELEASE_TAG}" dist/* --clobber' in workflow_text
