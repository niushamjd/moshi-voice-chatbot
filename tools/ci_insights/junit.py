"""Parse pytest JUnit XML, either plain or inside a GitHub artifact zip."""

import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"


@dataclass(frozen=True)
class TestCase:
    __test__ = False  # not a pytest test class

    id: str
    outcome: str
    time: float


def parse_junit_xml(data: bytes) -> list[TestCase]:
    cases = []
    for case in ET.fromstring(data).iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        test_id = f"{classname}::{name}" if classname else name
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = FAILED
        elif case.find("skipped") is not None:
            outcome = SKIPPED
        else:
            outcome = PASSED
        cases.append(TestCase(test_id, outcome, float(case.get("time") or 0)))
    return cases


def parse_junit_zip(data: bytes) -> list[TestCase]:
    cases = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if name.endswith(".xml"):
                cases.extend(parse_junit_xml(archive.read(name)))
    return cases
