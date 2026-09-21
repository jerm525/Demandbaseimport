import pytest

from demandbase_sync.mapping import (
    FieldMapping,
    MappingValidationError,
    RecordMappingError,
    apply_mapping,
    validate_mapping,
)

from .conftest import VIEW_COLUMNS, make_source_record


def test_validate_mapping_passes(test_mapping):
    validate_mapping(test_mapping, VIEW_COLUMNS)


def test_validate_mapping_fails_on_unknown_source_column(test_mapping):
    bad_entry = FieldMapping(source_field="DoesNotExist", target_field="Territory", transform="none")
    mapping = [m for m in test_mapping if m.target_field != "Territory"] + [bad_entry]
    with pytest.raises(MappingValidationError, match="DoesNotExist"):
        validate_mapping(mapping, VIEW_COLUMNS)


def test_validate_mapping_fails_on_missing_required_target(test_mapping):
    mapping = [m for m in test_mapping if m.target_field != "Stage"]
    with pytest.raises(MappingValidationError, match="Stage"):
        validate_mapping(mapping, VIEW_COLUMNS)


def test_apply_mapping_success(test_mapping):
    record = make_source_record()
    result = apply_mapping(record, test_mapping)
    assert result["SFDC_OpportunityId"] == record["SFDC_ID"]
    assert result["account_name"] == "Acme Corp"
    assert result["Is Closed?"] == "N"
    assert result["amount_mrr"] == 1000.56 or result["amount_mrr"] == 1000.55  # rounding is fine either way


def test_apply_mapping_required_field_null_raises(test_mapping):
    # TC8: a record with a null required field fails that record only.
    record = make_source_record(AccountName=None)
    with pytest.raises(RecordMappingError) as exc_info:
        apply_mapping(record, test_mapping)
    assert exc_info.value.field == "account_name"


def test_apply_mapping_optional_field_null_uses_default(test_mapping):
    record = make_source_record(Territory=None)
    result = apply_mapping(record, test_mapping)
    assert result["Territory"] == ""
