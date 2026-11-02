"""Print an unapplied, image-only override; never invokes Docker or a network."""

from __future__ import annotations

import argparse
import json
import re


def candidate(image: str) -> dict:
    # A local image ID or registry digest is immutable. Tags, whitespace and
    # option-like input cannot silently select or pull an unrelated image.
    if not re.fullmatch(
        r"(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:[a-f0-9]{64}", image
    ):
        raise ValueError(
            "Provide an immutable local image ID or repository@sha256 digest"
        )
    return {"services": {"freeswitch": {"image": image, "pull_policy": "never"}}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    try:
        override = candidate(args.image)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(override, indent=2))


if __name__ == "__main__":
    main()
