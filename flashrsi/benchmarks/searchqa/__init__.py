"""SearchQA dataset loading and local grading."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from flashrsi.benchmarks._qa import QABenchmark
from flashrsi.core.benchmark import Task


class SearchQA(QABenchmark):
    """SearchQA with deterministic configured splits and local EM/F1 grading."""

    name = "searchqa"
    _default_scoring = "text_em_f1"
    _scoring_modes = ("text_em_f1", "legacy")
    description = "Open-domain question answering over retrieved search snippets."
    metadata = MappingProxyType(
        {
            "source": "kyunghyuncho/search_qa",
            "config": "train_test_val",
            "revision": "06907e45883b7cae435453b65d598447039fde79",
        }
    )

    def load(self):
        if self._splits is not None:
            return self._splits
        source_split = self.config.get("evolution_split")
        trivia = self.config.get("evolution_triviaqa")
        if trivia is not None:
            if source_split is not None:
                raise ValueError("SearchQA evolution sources are mutually exclusive")
            if not isinstance(trivia, Mapping) or trivia.get("split") not in (
                "train",
                "validation",
            ):
                raise ValueError(
                    "SearchQA evolution_triviaqa needs split=train/validation"
                )
            dataset = trivia.get("dataset", {})
            if not isinstance(dataset, Mapping):
                raise ValueError(
                    "SearchQA evolution_triviaqa.dataset must be a source mapping"
                )
            from flashrsi.benchmarks.triviaqa import TriviaQA

            source = {**TriviaQA.metadata, **dataset, "splits": (trivia["split"],)}
            # Reuse native questions, evidence and aliases. SearchQA retains its
            # own text scorer rather than claiming TriviaQA official grading.
            source.pop("scorer_revision", None)
            external = TriviaQA({"dataset": source, "scoring": "legacy"})
            self._load_evolution(external, trivia["split"])
            return self._splits
        if source_split is None:
            if self.config.get("evolution_limit") is not None:
                raise ValueError("SearchQA evolution_limit requires evolution_split")
            return super().load()
        if source_split not in ("train", "validation", "test"):
            raise ValueError("SearchQA evolution_split must be train/validation/test")
        external = SearchQA(
            {"dataset": {**type(self).metadata, "splits": (source_split,)}}
        )
        self._load_evolution(external, source_split)
        return self._splits

    def _load_source_rows(self):
        source = self._source_metadata()
        if (
            self.config.get("data_path") is not None
            or source["source"] != "kyunghyuncho/search_qa"
        ):
            return super()._load_source_rows()
        self.metadata = source
        return self._native_archives(source)

    @staticmethod
    def _native_archives(source):
        from huggingface_hub import hf_hub_download

        if source.get("config", "train_test_val") != "train_test_val":
            raise ValueError(
                "SearchQA native archive loading supports config=train_test_val"
            )
        splits = source.get("splits", ("train", "validation", "test"))
        if (
            not isinstance(splits, (list, tuple))
            or not splits
            or set(splits) - {"train", "validation", "test"}
        ):
            raise ValueError(
                "SearchQA dataset.splits must select train/validation/test"
            )
        # Read the author's native JSON archives without executing the obsolete
        # datasets loading script or publishing a converted experiment dataset.
        for split in splits:
            name = "val" if split == "validation" else split
            path = hf_hub_download(
                repo_id=source["source"],
                repo_type="dataset",
                revision=source["revision"],
                filename=f"data/train_test_val/{name}.zip",
            )
            with zipfile.ZipFile(path) as archive:
                for member in sorted(archive.namelist()):
                    if member.endswith(".json"):
                        yield split, json.loads(archive.read(member))

    @staticmethod
    def _task_from_row(row: Mapping[str, Any], source_split: str) -> Task:
        task_id = row.get("id", row.get("key"))
        question = row.get("question")
        context = row.get("context")
        if context is None and "search_results" in row:
            results = row["search_results"]
            snippets = (
                results.get("snippets")
                if isinstance(results, Mapping)
                else [item.get("snippet") for item in results]
                if isinstance(results, list)
                and all(isinstance(item, Mapping) for item in results)
                else None
            )
            if not isinstance(snippets, list) or any(
                snippet is not None and not isinstance(snippet, str)
                for snippet in snippets
            ):
                raise ValueError(
                    "SearchQA native search_results has malformed snippets"
                )
            # Native search results include pages without a snippet (JSON null).
            context = "\n".join(snippet for snippet in snippets if snippet is not None)
            if type(task_id) is int:
                task_id = f"{source_split}:{task_id}"
            if task_id is None:
                task_id = hashlib.sha256(
                    json.dumps(row, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
        answers = row.get("answers", row.get("answer"))
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("SearchQA row has no non-empty key/id")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"SearchQA row {task_id} has no question")
        if not isinstance(context, str):
            raise ValueError(f"SearchQA row {task_id} context must be a string")
        if isinstance(answers, str):
            answers = (answers,)
        elif isinstance(answers, Sequence) and not isinstance(
            answers, (bytes, bytearray)
        ):
            answers = tuple(answers)
        else:
            answers = ()
        if not answers or any(
            not isinstance(answer, str) or not answer.strip() for answer in answers
        ):
            raise ValueError(
                f"SearchQA row {task_id} answers must be non-empty strings"
            )
        prompt = (
            "Answer the question using the supplied context. Return only the short "
            f"answer.\n\nContext:\n{context}\n\nQuestion:\n{question}"
        )
        return Task(
            task_id=task_id,
            question=prompt,
            ground_truth=answers[0],
            env="supplied_context",
            metadata={
                "answers": answers,
                "source_split": source_split,
                "question_checksum": hashlib.sha256(
                    " ".join(question.split()).casefold().encode()
                ).hexdigest(),
            },
        )


__all__ = ["SearchQA"]
