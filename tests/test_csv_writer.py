import csv
import datetime as dt

from demandbase_sync.csv_writer import build_csv_filename, write_csv
from demandbase_sync.mapping import apply_mapping

from .conftest import make_source_record


def test_write_csv_headers_and_rows(tmp_path, test_mapping):
    records = [make_source_record(sfdc_id="A1"), make_source_record(sfdc_id="A2")]
    mapped = [apply_mapping(r, test_mapping) for r in records]

    path = tmp_path / "out.csv"
    write_csv(mapped, test_mapping, path)

    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        assert reader.fieldnames == [m.target_field for m in test_mapping]

    assert len(rows) == 2
    assert rows[0]["SFDC_OpportunityId"] == "A1"
    assert rows[1]["SFDC_OpportunityId"] == "A2"


def test_build_csv_filename_includes_job_id_to_avoid_same_day_collision():
    d = dt.date(2026, 9, 18)
    name1 = build_csv_filename("job-111", d)
    name2 = build_csv_filename("job-222", d)
    assert name1 != name2
    assert "20260918" in name1
    assert "job-111" in name1
