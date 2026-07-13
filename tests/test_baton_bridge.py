"""Tests for carry-protocol baton bridge."""

import pytest
from carry.baton_bridge import (
    BatonLessonPayload,
    pack_lessons,
    unpack_lessons,
)


@pytest.fixture
def sample_lessons():
    return [
        {
            "lesson_id": "lesson-001",
            "content": "Model X hallucinates when asked about Y",
            "confidence": 0.85,
            "created_at": "2026-07-13T05:00:00Z",
            "model_origin": "glm-5.2",
            "tags": ["hallucination", "safety"],
        },
        {
            "lesson_id": "lesson-002",
            "content": "Always verify file paths before writing",
            "confidence": 0.92,
            "created_at": "2026-07-13T05:01:00Z",
            "model_origin": "glm-5.2",
            "tags": ["filesystem", "safety"],
            "expires_at": "2027-07-13T00:00:00Z",
        },
    ]


@pytest.fixture
def single_lesson():
    return [{
        "lesson_id": "lesson-single",
        "content": "Test lesson",
        "confidence": 0.5,
    }]


class TestBatonLessonPayload:
    def test_round_trip(self):
        d = {"lesson_id": "x", "content": "test", "confidence": 0.9}
        payload = BatonLessonPayload.from_dict(d)
        assert payload.lesson_id == "x"
        out = payload.to_dict()
        assert out["lesson_id"] == "x"
        assert out["confidence"] == 0.9

    def test_defaults(self):
        d = {"lesson_id": "x", "content": "test"}
        payload = BatonLessonPayload.from_dict(d)
        assert payload.confidence == 0.5
        assert payload.tags == []
        assert payload.metadata == {}


class TestPackUnpack:
    def test_round_trip(self, sample_lessons):
        parcel = pack_lessons(sample_lessons, route="satellite")
        assert parcel["type"] == "baton_lessons"
        assert parcel["count"] == 2
        assert parcel["route"] == "satellite"

        unpacked = unpack_lessons(parcel)
        assert len(unpacked) == 2
        assert unpacked[0].lesson_id == "lesson-001"
        assert unpacked[1].lesson_id == "lesson-002"

    def test_checksum_catches_corruption(self, sample_lessons):
        parcel = pack_lessons(sample_lessons)
        # Corrupt a lesson
        parcel["lessons"][0]["content"] = "TAMPERED"
        with pytest.raises(ValueError, match="Checksum mismatch"):
            unpack_lessons(parcel)

    def test_wrong_type_raises(self):
        with pytest.raises(ValueError, match="Expected payload type"):
            unpack_lessons({"type": "wrong", "lessons": []})

    def test_empty_lessons(self):
        parcel = pack_lessons([])
        assert parcel["count"] == 0
        unpacked = unpack_lessons(parcel)
        assert len(unpacked) == 0

    def test_single_lesson(self, single_lesson):
        parcel = pack_lessons(single_lesson)
        unpacked = unpack_lessons(parcel)
        assert len(unpacked) == 1
        assert unpacked[0].content == "Test lesson"

    def test_preserves_optional_fields(self, sample_lessons):
        parcel = pack_lessons(sample_lessons)
        unpacked = unpack_lessons(parcel)
        assert unpacked[1].expires_at == "2027-07-13T00:00:00Z"
        assert "hallucination" in unpacked[0].tags
        assert unpacked[0].model_origin == "glm-5.2"

    def test_checksum_is_deterministic(self, sample_lessons):
        p1 = pack_lessons(sample_lessons)
        p2 = pack_lessons(sample_lessons)
        assert p1["checksum"] == p2["checksum"]

    def test_different_routes(self, sample_lessons):
        p1 = pack_lessons(sample_lessons, route="usb")
        p2 = pack_lessons(sample_lessons, route="lora")
        assert p1["route"] != p2["route"]
        # Same lessons, different route — checksum should be same (based on data only)
        assert p1["checksum"] == p2["checksum"]
