"""Strict calculation records; these checks do not prove business correctness."""
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .contracts import validate_answer

Text = Annotated[str, Field(min_length=1)]


class Period(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    start: str
    end: str

    @model_validator(mode="after")
    def valid_dates(self):
        start, end = date.fromisoformat(self.start), date.fromisoformat(self.end)
        if start.isoformat() != self.start or end.isoformat() != self.end:
            raise ValueError("Period dates must use YYYY-MM-DD")
        if start > end:
            raise ValueError("Period start must not exceed end")
        return self


class Calculation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    sources: Annotated[list[Text], Field(min_length=1)]
    metric: Text
    period: Period | None
    grain: Text
    unit: Text
    rules: Annotated[list[Text], Field(min_length=1)]
    row_count: Annotated[int, Field(ge=0)]
    checks: Annotated[dict[str, JsonValue], Field(min_length=1)]
    date_min: str | None = None
    date_max: str | None = None
    missing: str | None = None
    search_complete: bool = False

    @model_validator(mode="after")
    def valid_coverage(self):
        if self.date_min is not None and self.date_max is not None:
            first, last = date.fromisoformat(self.date_min), date.fromisoformat(self.date_max)
            if (first.isoformat() != self.date_min or last.isoformat() != self.date_max
                    or first > last):
                raise ValueError("Coverage must use ordered YYYY-MM-DD dates")
        return self


def validate_candidate(candidate: dict, reads: list[dict]) -> dict:
    """Validate final output against full reads from the current worker session."""
    answer = validate_answer(candidate)
    if set(candidate) - {"type", "answer", "reason", "calculation"}:
        raise ValueError("Unexpected final answer fields")
    if answer["type"] == "abstain":
        return answer
    calculation = Calculation.model_validate(candidate.get("calculation"))
    opened = {read["source"] for read in reads if read.get("full") is True}
    if not set(calculation.sources) <= opened:
        raise ValueError("Read every declared source with read_table before finalizing")
    if answer["type"] == "unanswerable":
        if not calculation.search_complete or not calculation.missing:
            raise ValueError("unanswerable needs a complete source diagnostic and missing information")
    elif calculation.period and calculation.row_count:
        if calculation.date_min is None or calculation.date_max is None:
            raise ValueError("Dated nonempty calculations require date_min and date_max")
        if not calculation.period.start <= calculation.date_min <= calculation.date_max <= calculation.period.end:
            raise ValueError("Reported selected dates must lie within the requested period")
    return answer
