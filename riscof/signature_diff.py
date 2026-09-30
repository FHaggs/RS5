#!/usr/bin/env python3

import argparse
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class SignatureWord:
    hexadecimal: str
    unsigned: int
    signed: int


def parse_signature(path: Path, word_bits: int) -> list[SignatureWord]:
    maximum = 1 << word_bits
    sign_bit = 1 << (word_bits - 1)
    words = []

    for line_number, line in enumerate(
        path.read_text(encoding="ascii").splitlines(), start=1
    ):
        text = line.strip()
        if not text:
            continue
        try:
            value = int(text, 16)
        except ValueError as error:
            raise ValueError(
                f"{path}:{line_number}: invalid hexadecimal word {text!r}"
            ) from error
        if value >= maximum:
            raise ValueError(
                f"{path}:{line_number}: value {text!r} exceeds {word_bits} bits"
            )

        signed = value - maximum if value & sign_bit else value
        words.append(
            SignatureWord(
                hexadecimal=f"0x{value:0{word_bits // 4}x}",
                unsigned=value,
                signed=signed,
            )
        )

    return words


def format_word(word: SignatureWord | None) -> str:
    if word is None:
        return f"{'<missing>':<18} {'-':>10} {'-':>11}"
    return f"{word.hexadecimal:<18} {word.unsigned:>10} {word.signed:>11}"


def compare_signatures(
    left_words: Sequence[SignatureWord],
    right_words: Sequence[SignatureWord],
    left_label: str,
    right_label: str,
) -> bool:
    print(
        f"{'word':>4}  {'status':<5}  "
        f"{left_label + ' (hex / unsigned / signed)':<42}  "
        f"{right_label + ' (hex / unsigned / signed)'}"
    )
    print("-" * 100)

    identical = True
    for index, (left, right) in enumerate(
        zip_longest(left_words, right_words)
    ):
        matches = left is not None and right is not None and left == right
        status = "MATCH" if matches else "DIFF"
        identical &= matches
        print(
            f"{index:>4}  {status:<5}  {format_word(left)}  {format_word(right)}"
        )

    if not left_words and not right_words:
        print("(both signatures are empty)")

    print()
    print("Signatures match." if identical else "Signatures differ.")
    return identical


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare two RISCOF signatures and show every word as hexadecimal, "
            "unsigned decimal, and signed decimal."
        )
    )
    parser.add_argument("left", type=Path, help="first signature file")
    parser.add_argument("right", type=Path, help="second signature file")
    parser.add_argument(
        "--word-bits",
        type=int,
        choices=(32, 64),
        default=32,
        help="signature word width (default: 32)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        left_words = parse_signature(args.left, args.word_bits)
        right_words = parse_signature(args.right, args.word_bits)
    except (OSError, ValueError) as error:
        print(f"Signature comparison failed: {error}")
        return 2

    identical = compare_signatures(
        left_words,
        right_words,
        args.left.name,
        args.right.name,
    )
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
