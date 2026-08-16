#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Assert on the runner what the consumer will assert after retrieval.

    python3 verify.py --out out

The archive is hashed here and hashed again once Claude has pulled it through the GitHub
connector. A truncated transfer and a clean download look identical unless both ends agree
on a digest, so both ends compute one.
"""

import argparse
import hashlib
import json
import os
import sys
import zipfile


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify the run before it leaves the runner.")
    ap.add_argument("--out", default="out")
    args = ap.parse_args(argv)
    out = os.path.abspath(args.out)

    with open(os.path.join(out, "summary.json"), encoding="utf-8") as fh:
        summary = json.load(fh)
    with open(os.path.join(out, "normalized", "manifest_normalized.json"), encoding="utf-8") as fh:
        normalized = json.load(fh)

    archive = os.path.join(out, summary["originals_zip"])
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            print("FAIL: originals archive failed its integrity check", file=sys.stderr)
            return 2
    digest = sha256_file(archive)
    if digest != summary["originals_sha256"]:
        print("FAIL: originals sha256 changed after writing (%s != %s)"
              % (digest[:16], summary["originals_sha256"][:16]), file=sys.stderr)
        return 2

    summary.update({"normalized_entries": normalized["entries"],
                    "normalized_markdown": normalized["markdown"],
                    "normalized_unsupported": normalized["unsupported"],
                    "normalized_chars": normalized["chars"]})
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("## EIS %s\n\n"
                     "| | |\n|---|---|\n"
                     "| records | %d |\n| source files | %d |\n| bytes | %s |\n"
                     "| markdown documents | %d |\n| unsupported | %d |\n"
                     "| markdown characters | %s |\n| originals sha256 | `%s` |\n"
                     % (summary["procurement_id"], summary["records"], summary["files"],
                        f"{summary['bytes']:,}", normalized["markdown"],
                        normalized["unsupported"], f"{normalized['chars']:,}",
                        summary["originals_sha256"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
