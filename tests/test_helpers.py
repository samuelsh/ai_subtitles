from app import (
    allowed_file,
    chunk_offsets,
    response_format_to_ext,
    TEN_MINUTES,
    UPLOAD_LIMIT_BYTES,
    CHUNK_CONCURRENCY,
)


class TestAllowedFile:
    def test_accepts_all_supported_extensions(self):
        for name in ("a.mp3", "A.MP3", "clip.mp4", "x.wav", "y.ogg", "z.m4a", "a.aac", "b.flac", "c.webm"):
            assert allowed_file(name)

    def test_rejects_empty(self):
        assert allowed_file("") is False
        assert allowed_file(None) is False

    def test_rejects_unsupported(self):
        assert allowed_file("foo.txt") is False
        assert allowed_file("noext") is False
        assert allowed_file("movie.mov") is False


class TestResponseFormatToExt:
    def test_known_formats(self):
        assert response_format_to_ext("text") == "txt"
        assert response_format_to_ext("srt") == "srt"
        assert response_format_to_ext("vtt") == "vtt"
        assert response_format_to_ext("json") == "json"
        assert response_format_to_ext("verbose_json") == "json"

    def test_unknown_format_defaults_to_txt(self):
        assert response_format_to_ext("weird") == "txt"
        assert response_format_to_ext("") == "txt"


class TestChunkOffsets:
    def test_zero_length(self):
        assert list(chunk_offsets(0)) == []

    def test_negative(self):
        assert list(chunk_offsets(-5)) == []

    def test_exact_multiple_does_not_add_empty_chunk(self):
        # 60 min -> 6 chunks, no extra trailing chunk
        offsets = list(chunk_offsets(6 * TEN_MINUTES))
        assert offsets == [0, 1, 2, 3, 4, 5]

    def test_partial_tail_is_covered(self):
        # 25 min -> 3 chunks (0-10, 10-20, 20-25); the old code dropped the
        # last 5 minutes.
        offsets = list(chunk_offsets(25 * 60))
        assert offsets == [0, 1, 2]

    def test_just_over_a_boundary(self):
        offsets = list(chunk_offsets(TEN_MINUTES + 1))
        assert offsets == [0, 1]

    def test_custom_chunk_seconds(self):
        assert list(chunk_offsets(30, chunk_seconds=10)) == [0, 1, 2]
        assert list(chunk_offsets(30, chunk_seconds=15)) == [0, 1]


class TestConfigDefaults:
    def test_upload_limit_is_2_gib(self):
        assert UPLOAD_LIMIT_BYTES == 2 * 1024 * 1024 * 1024

    def test_chunk_concurrency_is_4(self):
        assert CHUNK_CONCURRENCY == 4