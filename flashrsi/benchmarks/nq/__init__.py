"""Natural Questions contextual short-answer subset and local EM/F1 grading."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from flashrsi.benchmarks._qa import QABenchmark
from flashrsi.core.benchmark import Task


class NQ(QABenchmark):
    """Keep questions with a textual short answer or yes/no annotation.

    Full page text is supplied without answer windows; each annotation's spans
    form one complete reference, rather than independently acceptable answers.
    """

    _default_scoring = "short_answer_text"
    _scoring_modes = ("short_answer_text", "legacy")

    name = "nq"
    description = "Natural Questions short-answer QA over supplied Wikipedia pages."
    metadata = MappingProxyType(
        {
            "source": "google-research-datasets/natural_questions",
            "config": "default",
            "revision": "e8103d566bef4154c2c12b17c6095ec5275840cc",
            "subset": "any-short-answer-or-yes-no",
            "split_policy": "user-configured",
            "splits": ("train", "validation"),
        }
    )

    def _load_source_rows(self):
        rows = super()._load_source_rows()
        self.metadata = MappingProxyType(
            {
                **self.metadata,
                "subset": "mrqa-v2"
                if self.config.get("_prepared_mrqa")
                else type(self).metadata["subset"],
            }
        )
        return rows

    def _iter_tasks(self) -> Iterable[Task]:
        if self.config.get("_prepared_mrqa"):
            yield from super()._iter_tasks()
            return
        # Filtering belongs to this subset, not to the shared QA contract.
        for source, row in self._load_source_rows():
            task = self._task_from_row(row, source)
            if task is not None:
                yield task

    @staticmethod
    def _task_from_row(row: Mapping[str, Any], source_split: str) -> Task | None:
        """Return None only for a well-formed row with no short-answer annotation."""
        if not isinstance(row, Mapping):
            raise ValueError("NQ rows must be mappings")
        task_id = row.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("NQ row has no non-empty id")
        question = row.get("question")
        question = question.get("text") if isinstance(question, Mapping) else None
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"NQ row {task_id} has no question text")
        document = row.get("document")
        tokens = document.get("tokens") if isinstance(document, Mapping) else None
        if not isinstance(tokens, Mapping):
            raise ValueError(f"NQ row {task_id} has malformed document tokens")
        words, html = tokens.get("token"), tokens.get("is_html")
        if (
            not isinstance(words, list)
            or not isinstance(html, list)
            or not words
            or len(words) != len(html)
            or any(not isinstance(word, str) for word in words)
            or any(type(flag) is not bool for flag in html)
        ):
            raise ValueError(f"NQ row {task_id} has malformed document tokens")
        annotations = row.get("annotations")
        if not isinstance(annotations, Mapping):
            raise ValueError(f"NQ row {task_id} has malformed annotations")
        shorts, yes_no = (
            annotations.get("short_answers"),
            annotations.get("yes_no_answer"),
        )
        if (
            not isinstance(shorts, list)
            or not isinstance(yes_no, list)
            or not shorts
            or len(shorts) != len(yes_no)
        ):
            raise ValueError(f"NQ row {task_id} has malformed annotations")
        references = []
        for short, label in zip(shorts, yes_no, strict=True):
            if (
                not isinstance(short, Mapping)
                or type(label) is not int
                or label not in (-1, 0, 1)
            ):
                raise ValueError(
                    f"NQ row {task_id} has malformed short-answer annotation"
                )
            starts, ends = short.get("start_token"), short.get("end_token")
            if (
                not isinstance(starts, list)
                or not isinstance(ends, list)
                or len(starts) != len(ends)
            ):
                raise ValueError(f"NQ row {task_id} has malformed short-answer spans")
            if label != -1 and starts:
                raise ValueError(
                    f"NQ row {task_id} mixes yes/no and textual short answers"
                )
            spans = []
            for start, end in zip(starts, ends, strict=True):
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not 0 <= start < end <= len(words)
                ):
                    raise ValueError(
                        f"NQ row {task_id} has invalid short-answer token offsets"
                    )
                span = " ".join(
                    word
                    for word, tag in zip(words[start:end], html[start:end], strict=True)
                    if not tag
                )
                if not span.strip():
                    raise ValueError(f"NQ row {task_id} has an empty short-answer span")
                spans.append((start, span))
            if label != -1:
                references.append("yes" if label == 1 else "no")
            elif spans:
                # NQ annotation lists can be unordered; the prompt asks for
                # document order, so references must use token positions too.
                references.append(
                    " ".join(
                        span for _, span in sorted(spans, key=lambda item: item[0])
                    )
                )
        context = " ".join(
            word for word, tag in zip(words, html, strict=True) if not tag
        )
        if not context.strip():
            raise ValueError(f"NQ row {task_id} has no document text")
        if not references:
            return None
        return Task(
            task_id=task_id,
            question=(
                "Answer the question using the supplied Wikipedia context. "
                "Return only the short answer. For multiple answer spans, list all "
                "of them in document order separated by spaces. "
                "For yes/no questions, return yes or no.\n\n"
                f"Context:\n{context}\n\nQuestion:\n{question}"
            ),
            ground_truth=references[0],
            env="supplied_context",
            metadata={"answers": tuple(references), "source_split": source_split},
        )


__all__ = ["NQ"]
