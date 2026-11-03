"""Run the selected harness in a container with only the public corpus."""

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from flashrsi.integrations import supplied_context


def bind(agent, benchmark):
    supplied_context._check(agent)
    value = benchmark.config.get("corpus_path")
    if not value:
        raise ValueError(
            "OfficeQA-Pro requires benchmark.corpus_path with public corpus documents"
        )
    corpus = Path(value).expanduser()
    if (
        not corpus.is_dir()
        or corpus.is_symlink()
        or (hasattr(corpus, "is_junction") and corpus.is_junction())
    ):
        raise ValueError("benchmark.corpus_path must be a public corpus directory")
    # Only the corpus is staged. A question/answer CSV in the parent directory
    # must not accidentally become part of the Agent's searchable workspace.
    corpus = corpus.resolve()
    entries = tuple(sorted(corpus.rglob("*")))
    documents = tuple(p for p in entries if p.is_file())
    if not documents or any(
        p.is_symlink()
        or (hasattr(p, "is_junction") and p.is_junction())
        or (
            not p.is_dir()
            and (
                not p.is_file()
                or p.suffix.lower() not in {".txt", ".md", ".pdf", ".json"}
            )
        )
        for p in entries
    ):
        raise ValueError(
            "corpus_path must contain only public txt/md/pdf/json documents"
        )

    async def run(target, task):
        if benchmark._tasks_by_id.get(task.task_id) != task:
            raise ValueError("OfficeQA-Pro requires a loaded benchmark Task")
        with TemporaryDirectory(prefix="flashrsi-officeqa-") as temporary:
            root = Path(temporary)
            destination = root / "corpus"
            # Copy, rather than link, so candidate edits cannot contaminate later runs.
            for path in documents:
                copied = destination / path.relative_to(corpus)
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, copied)
            return await supplied_context._run_prompt(
                target, task.question + "\n\nCorpus: ./corpus", workspace=root
            )

    agent._env_runner = run
