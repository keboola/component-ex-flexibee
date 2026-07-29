from keboola.component.dao import BaseType

from component import _build_typed_schema


def test_build_typed_schema_maps_flexibee_types_to_base_types():
    columns = ["id", "kod", "sumOsv", "datObj", "lastUpdate", "postovniShodna", "zamekK"]
    types = {
        "id": "integer",
        "kod": "string",
        "sumOsv": "numeric",
        "datObj": "date",
        "lastUpdate": "datetime",
        "postovniShodna": "logic",
        "zamekK": "select",
    }
    schema = _build_typed_schema(columns, types)

    def base(col):
        return schema[col].data_types["base"].dtype

    assert base("id") == "INTEGER"
    assert base("kod") == "STRING"
    assert base("sumOsv") == "NUMERIC"
    assert base("datObj") == "DATE"
    assert base("lastUpdate") == "TIMESTAMP"
    assert base("postovniShodna") == "BOOLEAN"
    assert base("zamekK") == "STRING"


def test_build_typed_schema_marks_key_columns_primary_key_and_not_nullable():
    schema = _build_typed_schema(["id", "kod"], {"id": "integer", "kod": "string"}, ["id"])
    assert schema["id"].primary_key is True
    assert schema["id"].nullable is False
    assert schema["kod"].primary_key is False
    assert schema["kod"].nullable is True


def test_build_typed_schema_marks_evidence_specific_key_column():
    columns = ["idUcetniDenik", "doklad"]
    types = {"idUcetniDenik": "integer", "doklad": "string"}
    schema = _build_typed_schema(columns, types, ["idUcetniDenik"])
    assert schema["idUcetniDenik"].primary_key is True
    assert schema["idUcetniDenik"].nullable is False
    assert schema["doklad"].primary_key is False


def test_build_typed_schema_without_primary_key_marks_no_column():
    schema = _build_typed_schema(["ucet", "mena"], {"ucet": "string", "mena": "relation"}, [])
    assert all(col.primary_key is False for col in schema.values())


def test_build_typed_schema_falls_back_to_string_for_missing_or_unknown_types():
    schema = _build_typed_schema(["x", "y"], {"x": "ufo-type"})  # y absent entirely
    assert schema["x"].data_types["base"].dtype == "STRING"
    assert schema["y"].data_types["base"].dtype == "STRING"


def test_build_typed_schema_uses_relation_siblings_as_string():
    columns = ["mena", "mena_ref", "mena_showAs"]
    types = {"mena": "relation", "mena_ref": "string", "mena_showAs": "string"}
    schema = _build_typed_schema(columns, types)
    for col in columns:
        assert schema[col].data_types["base"].dtype == "STRING"


def test_build_typed_schema_uses_base_type_enum_classmethods():
    # Sanity: builder return is what BaseType.<x>() returns directly.
    schema = _build_typed_schema(["x"], {"x": "integer"})
    assert schema["x"].data_types == BaseType.integer()
