"""The hypothesis card shown in docs/protocol.md must stay a valid card."""

import re
import tomllib
from pathlib import Path

from finresearch.registry.models import HypothesisCard, Status

PROTOCOL = Path(__file__).resolve().parents[1] / "docs" / "protocol.md"


def test_protocol_example_is_a_valid_card() -> None:
    blocks = re.findall(r"```toml\n(.*?)```", PROTOCOL.read_text(encoding="utf-8"), flags=re.DOTALL)
    assert len(blocks) == 1
    card = HypothesisCard.model_validate(tomllib.loads(blocks[0]))
    assert card.id == "H-0000"
    assert card.status is Status.DRAFT
    assert card.plan.threshold_t == 3.0
