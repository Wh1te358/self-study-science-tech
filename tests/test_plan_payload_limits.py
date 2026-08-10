import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import server


def build_large_evidence_plan_payload() -> dict:
    files = [
        {
            "id": f"F-{index + 1:03d}",
            "name": f"Chapter {index + 1:02d} university physics reference material.pdf".ljust(95, "x"),
            "kind": "PDF",
            "size_bytes": 123_456,
            "pages": 30,
            "text_pages": 30,
            "answer_status": "unknown",
            "parse_status": "parsed",
        }
        for index in range(25)
    ]

    def source_ref(index: int) -> dict:
        return {
            "source_id": files[index % len(files)]["id"],
            "locator": f"page {index + 1}",
        }

    units = [
        {
            "id": f"KU-{index + 1:02d}",
            "title": f"Knowledge unit {index + 1} mechanics thermodynamics".ljust(95, "x"),
            "formula": ("F = ma; conservation relation and boundary conditions " * 4)[:220],
            "typical_question": ("Solve the representative multi-step exam problem " * 4)[:220],
            "prerequisite": ("Required prerequisite concepts and definitions " * 3)[:160],
            "source_refs": [source_ref(index), source_ref(index + 1)],
        }
        for index in range(18)
    ]
    uncertainties = [
        {
            "id": f"UN-{index + 1:02d}",
            "description": (f"Unconfirmed exam scope or question type {index + 1} " * 5)[:250],
            "source_refs": [source_ref(index)],
        }
        for index in range(21)
    ]
    return {
        "course": "University Physics",
        "days_left": 10,
        "hours_per_day": 4,
        "goal_score": 85,
        "keywords": ",".join(unit["title"] for unit in units)[:900],
        "ui_language": "en",
        "content_language": "en",
        "content_language_source": "manual",
        "input_language": "en",
        "input_language_confidence": 1,
        "input_source": "materials",
        "evidence_map": {
            "version": "course-evidence-map.v1",
            "map_mode": "ai_extracted",
            "evidence_level": "page_cited",
            "files": files,
            "knowledge_units": units,
            "exam_signals": [],
            "uncertainties": uncertainties,
            "exam_constraint": {
                "source_type": "not_provided",
                "knowledge_status": "not_provided",
                "question_types": [],
                "note": None,
            },
        },
    }


class PlanPayloadLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(
            os.environ,
            {
                "REQUIRE_APP_TOKEN": "false",
                "MAX_BODY_BYTES": "20000",
                "MAX_PLAN_BODY_BYTES": str(server.DEFAULT_PLAN_REQUEST_BYTES),
            },
        )
        cls.environment.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.environment.stop()

    def post(self, body: bytes):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            connection.request("POST", "/api/plan", body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def post_declared_length(self, declared_length: int):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            connection.putrequest("POST", "/api/plan")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(declared_length))
            connection.endheaders()
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_large_valid_evidence_map_reaches_plan_generation(self):
        body = json.dumps(build_large_evidence_plan_payload(), ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(body), 20_000)
        self.assertLess(len(body), server.DEFAULT_PLAN_REQUEST_BYTES)

        with patch.object(server, "call_model", return_value={"headline": "large map accepted"}) as call_model:
            status, response = self.post(body)

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(response["plan"]["headline"], "large map accepted")
        call_model.assert_called_once()

    def test_payload_above_plan_limit_is_rejected_before_generation(self):
        with patch.object(server, "call_model") as call_model:
            status, response = self.post_declared_length(server.DEFAULT_PLAN_REQUEST_BYTES + 1)

        self.assertEqual(status, 413)
        self.assertEqual(response, {"ok": False, "error": "Payload Too Large"})
        call_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
