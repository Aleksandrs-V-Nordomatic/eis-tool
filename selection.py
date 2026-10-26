"""Which files of a tender are downloaded.

Two decisions per file, both driven by a rules file the caller supplies:

    kind_of(...)   what kind of document the file is, from its extension, its name, the folder
                   or archive it sits in, the record title and the EIS document type
    decide(...)    download it, or skip it and keep its name

The rules file holds the patterns and the list of kinds that are skipped; see
`tests/fixtures/document_rules.json` for the shape.
"""

import json
import os
import re
import unicodedata

UNKNOWN = "Unknown"
DOWNLOAD = "download"
SKIP = "skip"

_EXT = re.compile(r"\.(pdf|docx?|xlsx?|xlsm|odt|ods|rtf|edoc|asice|zip|7z|rar|dwg|dxf|jpe?g|png"
                  r"|tiff?|gif|txt)\)?$", re.I)


def fold(text):
    """Lower case, no diacritics, separators as single spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[_\.\-\(\),;+]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extension(name):
    return os.path.splitext(name or "")[1].lower().lstrip(".")


class Rules:
    """A loaded rules file."""

    def __init__(self, data):
        self.extensions = {ext.lower(): kind for kind, exts in data.get("extensions", {}).items()
                           for ext in exts}
        generated = data.get("eis_generated") or {}
        self.generated_name = re.compile(generated["name"]) if generated.get("name") else None
        self.generated_record = re.compile(generated.get("record") or "$^")
        self.generated_kind = generated.get("kind", UNKNOWN)
        self.generic_record = re.compile(data["generic_record"]) if data.get("generic_record") else None
        self.rules = [(r["kind"], re.compile(r["pattern"]), bool(r.get("weak")), bool(r.get("late")))
                      for r in data.get("name_rules", [])]
        codes = data.get("section_codes") or {}
        self.codes = re.compile(codes["pattern"]) if codes.get("pattern") else None
        self.codes_kind = codes.get("kind", UNKNOWN)
        self.skip = set(data.get("skip", []))
        self.archives = {a.lower() for a in data.get("archives", ["zip", "7z", "rar"])}

    @classmethod
    def load(cls, source):
        if isinstance(source, dict):
            return cls(source)
        if os.path.exists(source):
            with open(source, encoding="utf-8") as fh:
                return cls(json.load(fh))
        return cls(json.loads(source))

    def is_archive(self, name):
        return extension(name) in self.archives

    def _by_name(self, name):
        """(kind, weak) from a name alone, or (None, False)."""
        base = _EXT.sub("", name or "")
        if self.generated_name and self.generated_name.match(base):
            return None, False
        folded = fold(base) + " | " + fold(re.sub(r"(?<=[a-z])_(?=[a-z])", "", base))
        for kind, pattern, weak, late in self.rules:
            if late:
                continue
            if pattern.search(folded):
                if weak:
                    break
                return kind, False
        if self.codes and self.codes.search(base):
            return self.codes_kind, False
        for kind, pattern, weak, late in self.rules:
            if (weak or late) and pattern.search(folded):
                return kind, weak
        return None, False

    def kind_of(self, name, folders=(), archive=None, record_title=None, type_title=None):
        """The document kind of one file. The first rule that fits decides."""
        ext_kind = self.extensions.get(extension(name))
        if ext_kind:
            return ext_kind
        base = _EXT.sub("", name or "")
        if (self.generated_name and self.generated_name.match(base)
                and self.generated_record.search(fold(record_title))):
            return self.generated_kind
        kind, weak = self._by_name(name)
        if kind and not weak:
            return kind
        if self.generic_record and self.generic_record.search(fold(record_title)):
            record_title = ""
        for context in (" / ".join(reversed(list(folders))), archive, record_title, type_title):
            if not context:
                continue
            found, found_weak = self._by_name(context)
            if found and not found_weak:
                return found
        return kind or UNKNOWN

    def decide(self, kind, section="actual"):
        """(download or skip, reason)."""
        if section != "actual":
            return SKIP, "replaced version in the archive section"
        if kind in self.skip:
            return SKIP, "kind %s is not read" % kind
        return DOWNLOAD, "kind %s is read" % kind
