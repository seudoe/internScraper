"""POST /analyze is read-only and returns the pipeline's full verdict for one stored internship."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class AnalyzeEndpointTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        os.environ.pop("INTERNSCRAPER_API_KEY", None)
        import app as service
        self.client = TestClient(service.app)

    def test_rejected_item_has_a_failed_validation_stage(self):
        res = self.client.post("/analyze", json={"source": "github", "internship": {"name": "No company or link"}})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["result"]["outcome"], "rejected")
        self.assertEqual(body["stages"][0]["stage"], "validation")
        self.assertEqual(body["stages"][0]["status"], "failed")

    def test_never_writes_or_triggers_the_vectoriser(self):
        item = {"name": "Dev Intern", "company": "Acme", "applyLink": "https://acme.example/jobs/1", "summary": "Build things", "deadlineDate": "2030-01-01T00:00:00"}
        with mock.patch("pipeline.apply_platform_verdicts") as writer, mock.patch("pipeline._trigger_platform_vectorizer") as vec:
            res = self.client.post("/analyze", json={"source": "ifind", "internship": item})
        self.assertEqual(res.status_code, 200)
        writer.assert_not_called()
        vec.assert_not_called()
        result = res.json()["result"]
        self.assertIn(result["outcome"], ("scored", "error"))
        if result["outcome"] == "scored":
            self.assertEqual(result["document"]["name"], "Dev Intern")
            self.assertIn("scamDetails", result)

    def test_requires_the_api_key_when_configured(self):
        with mock.patch.dict(os.environ, {"INTERNSCRAPER_API_KEY": "secret"}):
            self.assertEqual(self.client.post("/analyze", json={"internship": {}}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
