"""SuperGPQA (2025) graduate knowledge questions with an explicit letter protocol.

Dataset: m-a-p/SuperGPQA at 4430d4458112c7d4497fdcf94d7cc223313d6acf.
This scores final answer letters, not the upstream free-form/content parser or
discipline-macro leaderboard protocol. No judge model or execution is needed.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from flashrsi.benchmarks._qa import QABenchmark
from flashrsi.core import Task


class SuperGPQA(QABenchmark):
    name = "supergpqa"
    description = "SuperGPQA graduate knowledge, final-letter accuracy."
    primary_metric = "accuracy"
    _default_scoring = "answer_letter"
    _scoring_modes = ("answer_letter",)
    metadata = MappingProxyType(
        {
            "source": "m-a-p/SuperGPQA",
            "revision": "4430d4458112c7d4497fdcf94d7cc223313d6acf",
            "upstream": "https://github.com/SuperGPQA/SuperGPQA",
            "splits": ("test",),
            "aggregation": "sample_mean",
            "answer_protocol": "final_letter_v1",
            "data_license": "ODC-BY-1.0; underlying content retains its own licenses",
        }
    )

    def load(self):
        if self._splits is not None:
            return self._splits
        source = self.config.get("evolution_arc")
        if source is None:
            if self.config.get("evolution_limit") is not None:
                raise ValueError("SuperGPQA evolution_limit requires evolution_arc")
            return super().load()
        if (
            not isinstance(source, Mapping)
            or set(source) != {"subset", "split"}
            or source["subset"] not in ("ARC-Challenge", "ARC-Easy")
            or source["split"] not in ("train", "validation", "test")
        ):
            raise ValueError(
                "SuperGPQA evolution_arc needs a native ARC subset and split"
            )
        self._load_evolution(_ARC(source), source["split"])
        return self._splits

    def _load_source_rows(self):
        if self.config.get("data_path") is not None:
            yield from super()._load_source_rows()
            return
        from huggingface_hub import hf_hub_download

        self.metadata = self._source_metadata()
        path = hf_hub_download(
            repo_id=self.metadata["source"],
            repo_type="dataset",
            revision=self.metadata["revision"],
            filename="SuperGPQA-all.jsonl",
        )
        # The author's single evaluation pool has no native training partition.
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield "test", json.loads(line)

    @staticmethod
    def _task_from_row(row, source_split):
        for field in ("uuid", "question", "answer_letter", "answer"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"SuperGPQA row requires a nonempty {field}")
        options = row.get("options")
        if (
            not isinstance(options, list)
            or not 2 <= len(options) <= 10
            or any(not isinstance(s, str) or not s.strip() for s in options)
        ):
            raise ValueError("SuperGPQA row requires 2-10 nonempty options")
        letters = "ABCDEFGHIJ"[: len(options)]
        answer = row["answer_letter"]
        if answer not in tuple(letters) or options[ord(answer) - 65] != row["answer"]:
            raise ValueError("SuperGPQA answer_letter and answer must match an option")
        choices = "\n".join(
            f"{letter}. {option}"
            for letter, option in zip(letters, options, strict=True)
        )
        return Task(
            task_id=row["uuid"],
            question=(
                f"{row['question']}\n\n{choices}\n\n"
                "Choose the best option. You may reason before answering. "
                f"End with a line containing only the answer letter (A-{letters[-1]})."
            ),
            ground_truth=answer,
            env="supplied_context",
            metadata={
                "answers": (answer,),
                "source_split": source_split,
                "question_checksum": hashlib.sha256(
                    re.sub(r"\s+", " ", row["question"]).strip().casefold().encode()
                ).hexdigest(),
                **{
                    key: row[key]
                    for key in ("discipline", "field", "subfield", "difficulty")
                    if key in row
                },
            },
        )

    def _score(self, prediction, task):
        # Never search arbitrary reasoning for a correct letter: mentioning all
        # ten choices must not earn credit. Only the final nonempty line counts.
        lines = prediction.strip().splitlines()
        final = lines[-1].strip() if lines else ""
        match = re.fullmatch(r"(?:Answer:\s*)?([A-J])\.?", final, re.IGNORECASE)
        answer = match.group(1).upper() if match else None
        return {"accuracy": float(answer == task.ground_truth)}

    def _zero_scores(self):
        return {"accuracy": 0.0}


class _ARC(SuperGPQA):
    """External native science questions, using the same final-letter protocol."""

    metadata = MappingProxyType(
        {
            "source": "allenai/ai2_arc",
            "revision": "210d026faf9955653af8916fad021475a3f00453",
            "data_license": "CC-BY-SA-4.0",
            "answer_protocol": "final_letter_v1",
        }
    )

    def __init__(self, source):
        super().__init__({})
        self.metadata = MappingProxyType({**self.metadata, **source})

    def _load_source_rows(self):
        import pyarrow.parquet as pq
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(
            repo_id=self.metadata["source"],
            repo_type="dataset",
            revision=self.metadata["revision"],
            filename=(
                f"{self.metadata['subset']}/"
                f"{self.metadata['split']}-00000-of-00001.parquet"
            ),
        )
        for batch in pq.ParquetFile(path).iter_batches():
            for row in batch.to_pylist():
                yield self.metadata["split"], row

    @staticmethod
    def _task_from_row(row, source_split):
        choices = row.get("choices")
        labels = choices.get("label") if isinstance(choices, Mapping) else None
        options = choices.get("text") if isinstance(choices, Mapping) else None
        if (
            not isinstance(labels, list)
            or not isinstance(options, list)
            or len(labels) != len(options)
            or any(not isinstance(label, str) or not label for label in labels)
            or len(set(labels)) != len(labels)
            or row.get("answerKey") not in labels
        ):
            raise ValueError(
                "ARC choices and answerKey must identify one native option"
            )
        index = labels.index(row["answerKey"])
        # ARC includes numeric labels. Preserve option order and the referenced
        # answer text while exposing SuperGPQA's existing letter-only protocol.
        task = SuperGPQA._task_from_row(
            {
                "uuid": row.get("id"),
                "question": row.get("question"),
                "options": options,
                "answer_letter": chr(65 + index),
                "answer": options[index],
            },
            source_split,
        )
        return replace(
            task,
            metadata={
                **task.metadata,
                "native_choice_labels": labels,
                "native_answer_key": row["answerKey"],
            },
        )


__all__ = ["SuperGPQA"]
