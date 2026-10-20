from __future__ import annotations

from collections import Counter

from common.enums import StreamOutcome
from common.media.probe import ProbeData
from common.plan.models import FFmpegPlan
from common.plan.renderer import StreamLogEntry


def validate_duration(
    input_probe: ProbeData, output_probe: ProbeData, plan: FFmpegPlan, stream_log: list[StreamLogEntry]
) -> None:
    duration = output_probe.duration_s
    if duration < 0:
        msg = f"Output duration is invalid: {duration!r} seconds."
        raise ValueError(msg)
    # A discarded track can define the container duration without extending retained media.
    cleanup = (
        not plan.extras
        and any(entry.outcome == StreamOutcome.DROP for entry in stream_log)
        and not any(entry.outcome == StreamOutcome.TRANSCODE for entry in stream_log)
    )
    if not cleanup and duration < input_probe.duration_s * plan.expected.min_duration_ratio:
        minimum = input_probe.duration_s * plan.expected.min_duration_ratio
        msg = f"Output duration {duration:g} s is below the required {minimum:g} s for this stage."
        raise ValueError(msg)


def validate_streams(input_probe: ProbeData, output_probe: ProbeData, stream_log: list[StreamLogEntry]) -> None:
    dropped = {entry.stream_index for entry in stream_log if entry.outcome == StreamOutcome.DROP}
    # The renderer maps every input stream except explicit drops. Burning an external
    # subtitle does not remove embedded tracks, and cover art cannot replace real video.
    expected = Counter(
        (stream.codec_type, stream.codec_type == "video" and stream.disposition_attached_pic)
        for stream in input_probe.streams
        if stream.index not in dropped
    )
    actual = Counter(
        (stream.codec_type, stream.codec_type == "video" and stream.disposition_attached_pic)
        for stream in output_probe.streams
    )
    if actual != expected:
        raise ValueError("artifact stream counts do not match the plan's retained streams")
