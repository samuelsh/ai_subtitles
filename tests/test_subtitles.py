import json

from app import (
    format_srt_ts,
    format_vtt_ts,
    split_sentences,
    build_cues,
    render_srt,
    render_vtt,
    merge_parsed_chunks,
    render_subtitles,
)


class TestFormatTimestamps:
    def test_srt_zero(self):
        assert format_srt_ts(0) == "00:00:00,000"

    def test_srt_basic(self):
        assert format_srt_ts(3661.5) == "01:01:01,500"

    def test_srt_negative_clamps_to_zero(self):
        assert format_srt_ts(-5) == "00:00:00,000"

    def test_vtt_zero(self):
        assert format_vtt_ts(0) == "00:00:00.000"

    def test_vtt_basic(self):
        assert format_vtt_ts(3661.5) == "01:01:01.500"

    def test_srt_uses_comma_vtt_uses_dot(self):
        assert "," in format_srt_ts(1.5)
        assert "." in format_vtt_ts(1.5)


class TestSplitSentences:
    def test_empty(self):
        assert split_sentences("") == []
        assert split_sentences(None) == []

    def test_whitespace_only(self):
        assert split_sentences("   \n\t  ") == []

    def test_single_sentence(self):
        assert split_sentences("Hello world.") == ["Hello world."]

    def test_multiple_sentences(self):
        result = split_sentences("First. Second! Third? Fourth.")
        assert result == ["First.", "Second!", "Third?", "Fourth."]

    def test_keeps_terminators(self):
        for term in ".!?":
            assert split_sentences(f"x{term}") == [f"x{term}"]

    def test_preserves_internal_punctuation(self):
        assert split_sentences("Hi, there. Bye, now.") == ["Hi, there.", "Bye, now."]

    def test_no_terminator_returns_whole_text(self):
        assert split_sentences("no ending here") == ["no ending here"]

    def test_trims_whitespace(self):
        assert split_sentences("  A.   B.  ") == ["A.", "B."]


class TestBuildCues:
    def test_empty_segments(self):
        assert build_cues([]) == []
        assert build_cues(None) == []

    def test_single_segment_single_sentence(self):
        segs = [{"start": 0.0, "end": 5.0, "text": "Hello."}]
        cues = build_cues(segs)
        assert cues == [(0.0, 5.0, "Hello.")]

    def test_single_segment_multi_sentence_splits_proportionally(self):
        segs = [{"start": 0.0, "end": 10.0, "text": "Aa. Bb."}]
        cues = build_cues(segs)
        assert len(cues) == 2
        assert cues[0][0] == 0.0
        assert cues[1][1] == 10.0  # last sentence always ends at seg_end
        # Mid timestamp split: each sentence 3 chars, equal split -> 5s each.
        assert abs(cues[0][1] - 5.0) < 0.01
        assert abs(cues[1][0] - 5.0) < 0.01

    def test_last_sentence_gets_seg_end(self):
        segs = [{"start": 1.0, "end": 4.0, "text": "Short. Much longer sentence here."}]
        cues = build_cues(segs)
        assert cues[-1][1] == 4.0

    def test_cues_are_monotonic(self):
        segs = [
            {"start": 0.0, "end": 10.0, "text": "A. B. C."},
            {"start": 10.0, "end": 20.0, "text": "D. E."},
        ]
        cues = build_cues(segs)
        for i in range(1, len(cues)):
            assert cues[i][0] >= cues[i - 1][1] - 0.001

    def test_base_offset_shifts_all_cues(self):
        segs = [{"start": 1.0, "end": 4.0, "text": "Hi. Bye."}]
        cues = build_cues(segs, base_offset_seconds=600.0)
        assert cues[0][0] == 601.0
        assert cues[-1][1] == 604.0

    def test_skips_empty_text_segments(self):
        segs = [{"start": 0.0, "end": 5.0, "text": ""}, {"start": 5.0, "end": 10.0, "text": "Real."}]
        cues = build_cues(segs)
        assert cues == [(5.0, 10.0, "Real.")]

    def test_handles_missing_start_end(self):
        cues = build_cues([{"text": "No timing."}])
        assert cues == [(0.0, 0.0, "No timing.")]


class TestRenderSrt:
    def test_empty_cues(self):
        out = render_srt([])
        assert out.strip() == ""

    def test_basic_cue(self):
        out = render_srt([(0.0, 1.5, "Hello.")])
        lines = out.strip().split("\n")
        assert lines[0] == "1"
        assert lines[1] == "00:00:00,000 --> 00:00:01,500"
        assert lines[2] == "Hello."

    def test_indexing_is_sequential(self):
        out = render_srt([(0, 1, "a"), (1, 2, "b"), (2, 3, "c")])
        assert "1\n" in out
        assert "2\n" in out
        assert "3\n" in out


class TestRenderVtt:
    def test_starts_with_webvtt_header(self):
        out = render_vtt([])
        assert out.startswith("WEBVTT")

    def test_uses_dot_not_comma(self):
        out = render_vtt([(0, 1.5, "Hi.")])
        assert "00:00:00.000 --> 00:00:01.500" in out


class TestMergeParsedChunks:
    def test_single_chunk_passes_through_offsets(self):
        parsed = {"text": "abc", "language": "en", "duration": 60.0,
                  "segments": [{"start": 1.0, "end": 2.0, "text": "abc"}]}
        merged = merge_parsed_chunks([(0.0, parsed)])
        assert merged["text"] == "abc"
        assert merged["language"] == "en"
        assert merged["duration"] == 60.0
        assert merged["segments"][0]["start"] == 1.0
        assert merged["segments"][0]["end"] == 2.0

    def test_offsets_shift_segment_timestamps(self):
        # Two 10-min chunks; the second chunk's 0..30s segment should appear
        # at 600..630 in absolute time.
        c0 = {"text": "x", "duration": 600.0,
              "segments": [{"start": 0.0, "end": 10.0, "text": "x"}]}
        c1 = {"text": "y", "duration": 600.0,
              "segments": [{"start": 0.0, "end": 30.0, "text": "y"}]}
        merged = merge_parsed_chunks([(0.0, c0), (600.0, c1)])
        assert merged["segments"][0]["start"] == 0.0
        assert merged["segments"][1]["start"] == 600.0
        assert merged["segments"][1]["end"] == 630.0

    def test_concatenates_text(self):
        c0 = {"text": "first ", "segments": []}
        c1 = {"text": "second", "segments": []}
        merged = merge_parsed_chunks([(0.0, c0), (10.0, c1)])
        assert merged["text"] == "first second"

    def test_duration_is_max_end_time(self):
        c0 = {"text": "", "duration": 600.0, "segments": []}
        c1 = {"text": "", "duration": 90.0, "segments": []}
        merged = merge_parsed_chunks([(0.0, c0), (600.0, c1)])
        assert merged["duration"] == 690.0  # 600 + 90

    def test_ignores_non_dict_input(self):
        merged = merge_parsed_chunks([(0.0, "garbage"), (0.0, None)])
        assert merged["text"] == ""
        assert merged["segments"] == []


class TestRenderSubtitles:
    def test_text_format_returns_text_field(self):
        parsed = {"text": "the full transcript", "segments": [{"start": 0, "end": 1, "text": "x"}]}
        assert render_subtitles(parsed, "text") == "the full transcript"

    def test_json_format_returns_valid_json(self):
        parsed = {"text": "x", "segments": []}
        out = render_subtitles(parsed, "json")
        parsed_back = json.loads(out)
        assert parsed_back["text"] == "x"

    def test_srt_format_sentence_aligned(self):
        parsed = {
            "text": "Aa. Bb. Cc.",
            "segments": [{"start": 0.0, "end": 9.0, "text": "Aa. Bb. Cc."}],
        }
        out = render_subtitles(parsed, "srt")
        # Three sentences -> three cues, sequentially numbered.
        assert out.startswith("1\n00:00:00,000")
        assert "\n2\n" in out
        assert "\n3\n" in out
        assert "Aa." in out and "Bb." in out and "Cc." in out

    def test_vtt_format_sentence_aligned(self):
        parsed = {"text": "A. B.", "segments": [{"start": 0.0, "end": 2.0, "text": "A. B."}]}
        out = render_subtitles(parsed, "vtt")
        assert out.startswith("WEBVTT")
        assert "A." in out and "B." in out

    def test_chunked_merge_then_render_produces_monotonic_srt(self):
        c0 = {"text": "a", "duration": 600.0,
              "segments": [{"start": 0.0, "end": 10.0, "text": "First chunk ends here."}]}
        c1 = {"text": "b", "duration": 30.0,
              "segments": [{"start": 0.0, "end": 30.0, "text": "Second chunk sentence."}]}
        merged = merge_parsed_chunks([(0.0, c0), (600.0, c1)])
        out = render_subtitles(merged, "srt")
        # The last cue's end timestamp must be >= 600s (second chunk starts at 600).
        assert "00:10:00,000 --> 00:10:30,000" in out  # 600..630 absolute
        # Indices do not restart at 1 between chunks.
        assert "1\n" in out
        assert "2\n" in out