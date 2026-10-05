"""Offline deterministic client. Scripted scores are NOT attack measurements."""
import json

from ..models import AttackerOutput, EvaluationResult
from .grok_client import parse_output


class FixtureClient:
    def __init__(self):
        self.attacker_calls = 0
        self.evaluator_calls = 0

    def generate(self, messages, schema, **kwargs):
        if schema is AttackerOutput:
            self.attacker_calls += 1
            value = {
                "improvement": "Synthetic fixture candidate to exercise independent branch histories.",
                "attack_string": f"[SYNTHETIC TEST CANDIDATE {self.attacker_calls}: no real attack strategy]",
            }
        elif schema is EvaluationResult:
            self.evaluator_calls += 1
            value = {
                "score": float((self.evaluator_calls * 3) % 10),
                "attack_progress_summary": "SYNTHETIC score for pruning tests, unrelated to attack effectiveness.",
                "benign_task_summary": "No BUA ran; mock page read only.",
                "key_evidence": ["Mock trace and fixture_page_read action are synthetic."],
                "limiting_factor": "Real browser environment is not implemented.",
            }
        else:
            raise TypeError("Unsupported fixture schema")
        return parse_output(json.dumps(value), schema)
