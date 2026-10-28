"""Run compilation service behind the legacy model-facing façade."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iarf_assistant.core.execution_scope import ExecutionScope
from iarf_assistant.extensions import db
from iarf_assistant.models import WorkflowRun


def _clamped_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


@dataclass(slots=True)
class CompiledRunSpec:
    title: str
    delegation: dict[str, Any]
    delegation_plan: dict[str, Any]
    model_preferences: dict[str, Any]
    concurrency: dict[str, int]
    steps: list[dict[str, Any]]
    checkpoints: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    strategy: str
    compile_mode: str
    start_preference: str
    preview_preference: str
    complexity: dict[str, Any]
    preview_required: bool
    should_start: bool
    metadata: dict[str, Any]
    runtime_policy: dict[str, Any]
    goal: Any
    understanding: dict[str, Any]
    plan: dict[str, Any]


class RunCompiler:
    """Normalize, validate, and materialize one durable run definition."""

    def __init__(self, host: Any):
        self.host = host

    def compile(self, payload: dict[str, Any]) -> dict[str, Any]:
        spec = self._build_spec(payload)
        created = self._create_base_run(payload, spec)
        run_id = str(created["run"]["id"])
        steps = self._materialize_steps(spec, run_id)
        collection, partition = self._materialize_delegation(spec, run_id)
        sources = self._materialize_sources(spec, run_id)
        checkpoints = self._materialize_checkpoints(spec, run_id)
        status, run_payload, next_action = self._start_or_preview(spec, run_id)
        return self._result(
            spec,
            status=status,
            run_payload=run_payload,
            next_action=next_action,
            steps=steps,
            sources=sources,
            checkpoints=checkpoints,
            collection=collection,
            partition=partition,
        )

    def _build_spec(self, payload: dict[str, Any]) -> CompiledRunSpec:
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("title is required.")
        delegation, models = self._preferences(payload)
        concurrency = self._concurrency(payload, delegation)
        delegation_plan = self.host._compiled_delegation_plan(
            payload,
            delegation=delegation,
            model_preferences=models,
            concurrency=concurrency,
        )
        steps = [
            *self.host._compiled_step_list(payload),
            *(delegation_plan.get("steps") or []),
        ]
        checkpoints = self._dict_items(payload.get("checkpoints"))
        sources = self._dict_items(payload.get("sources"))
        strategy = self._strategy(payload, delegation_plan)
        compile_mode, start_preference, preview_preference = self._start_preferences(
            payload
        )
        complexity = self.host._compiled_run_complexity(
            steps=steps,
            checkpoints=checkpoints,
            sources=sources,
            strategy=strategy,
            policy=payload.get("policy")
            if isinstance(payload.get("policy"), dict)
            else {},
        )
        preview_required = self.host._compiled_run_requires_preview(
            complexity=complexity,
            start_preference=start_preference,
            preview_preference=preview_preference,
        )
        should_start = not preview_required and start_preference in {
            "start_now",
            "start_if_simple",
        }
        metadata, runtime_policy = self._metadata(
            payload,
            delegation_plan=delegation_plan,
            strategy=strategy,
            compile_mode=compile_mode,
            start_preference=start_preference,
            preview_preference=preview_preference,
            complexity=complexity,
            concurrency=concurrency,
            model_preferences=models,
        )
        self.host._validate_compiled_steps_against_scope(steps, metadata)
        goal, understanding, plan = self._understanding_and_plan(
            payload,
            steps=steps,
            checkpoints=checkpoints,
            delegation_plan=delegation_plan,
            concurrency=concurrency,
        )
        return CompiledRunSpec(
            title=title,
            delegation=delegation,
            delegation_plan=delegation_plan,
            model_preferences=models,
            concurrency=concurrency,
            steps=steps,
            checkpoints=checkpoints,
            sources=sources,
            strategy=strategy,
            compile_mode=compile_mode,
            start_preference=start_preference,
            preview_preference=preview_preference,
            complexity=complexity,
            preview_required=preview_required,
            should_start=should_start,
            metadata=metadata,
            runtime_policy=runtime_policy,
            goal=goal,
            understanding=understanding,
            plan=plan,
        )

    @staticmethod
    def _dict_items(value: Any) -> list[dict[str, Any]]:
        return [item for item in (value or []) if isinstance(item, dict)]

    @staticmethod
    def _strategy(
        payload: dict[str, Any],
        delegation_plan: dict[str, Any],
    ) -> str:
        value = (
            payload.get("execution_strategy")
            or payload.get("strategy")
            or ("bounded_map" if delegation_plan.get("steps") else "durable_run")
        )
        return str(value).strip() or "durable_run"

    @staticmethod
    def _preferences(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        delegation = (
            dict(payload["delegation"])
            if isinstance(payload.get("delegation"), dict)
            else {}
        )
        models = (
            dict(payload["model_preferences"])
            if isinstance(payload.get("model_preferences"), dict)
            else {}
        )
        if not models.get("worker_model") and (
            delegation.get("model") or delegation.get("model_role")
        ):
            models["worker_model"] = delegation.get("model") or delegation.get(
                "model_role"
            )
        reduction = (
            delegation["reduction"]
            if isinstance(delegation.get("reduction"), dict)
            else {}
        )
        if not models.get("reducer_model") and (
            reduction.get("model") or reduction.get("model_role")
        ):
            models["reducer_model"] = reduction.get("model") or reduction.get(
                "model_role"
            )
        return delegation, models

    def _concurrency(
        self,
        payload: dict[str, Any],
        delegation: dict[str, Any],
    ) -> dict[str, int]:
        raw = (
            dict(payload["concurrency"])
            if isinstance(payload.get("concurrency"), dict)
            else {}
        )
        for key in ("max_workers", "parallel_tool_calls"):
            if delegation.get(key) is not None and raw.get(key) is None:
                raw[key] = delegation[key]
        return {
            "max_workers": _clamped_int(
                raw.get("max_workers"),
                default=4,
                minimum=1,
                maximum=32,
            ),
            "parallel_tool_calls": _clamped_int(
                raw.get("parallel_tool_calls"),
                default=4,
                minimum=1,
                maximum=10,
            ),
        }

    @staticmethod
    def _start_preferences(payload: dict[str, Any]) -> tuple[str, str, str]:
        compile_mode = str(payload.get("compile_mode") or "").strip().lower()
        if compile_mode == "compile_only":
            start = "draft_only"
        elif compile_mode == "start":
            start = "start_now"
        else:
            start = str(payload.get("start_preference") or "start_if_simple").strip()
        preview = str(
            payload.get("preview_mode")
            or payload.get("preview_preference")
            or "auto"
        ).strip().lower()
        return compile_mode, start, preview

    @staticmethod
    def _runtime_policy(
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if isinstance(metadata.get("runtime_policy"), dict):
            return dict(metadata["runtime_policy"])
        if isinstance(payload.get("policy"), dict):
            return dict(payload["policy"])
        return {}

    def _metadata(
        self,
        payload: dict[str, Any],
        *,
        delegation_plan: dict[str, Any],
        strategy: str,
        compile_mode: str,
        start_preference: str,
        preview_preference: str,
        complexity: dict[str, Any],
        concurrency: dict[str, int],
        model_preferences: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata = (
            dict(payload["metadata"])
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        if "execution_scope" not in metadata and isinstance(
            payload.get("capability_scope"), dict
        ):
            metadata["execution_scope"] = ExecutionScope.from_dict(
                payload["capability_scope"]
            ).to_dict()
        runtime_policy = self._runtime_policy(payload, metadata)
        metadata["compiled_run"] = {
            "schema_version": 2,
            "strategy": strategy,
            "compile_mode": compile_mode or None,
            "start_preference": start_preference,
            "preview_mode": preview_preference,
            "complexity": complexity,
            "concurrency": concurrency,
            "model_preferences": model_preferences,
            "validation": payload.get("validation")
            if isinstance(payload.get("validation"), dict)
            else {},
            "retry_policy": payload.get("retry_policy")
            if isinstance(payload.get("retry_policy"), dict)
            else {},
            "delegation": delegation_plan.get("summary") or {},
        }
        metadata["max_concurrency"] = concurrency["max_workers"]
        if runtime_policy:
            metadata["runtime_policy"] = runtime_policy
            metadata["compiled_run"]["policy"] = runtime_policy
        return metadata, runtime_policy

    def _understanding_and_plan(
        self,
        payload: dict[str, Any],
        *,
        steps: list[dict[str, Any]],
        checkpoints: list[dict[str, Any]],
        delegation_plan: dict[str, Any],
        concurrency: dict[str, int],
    ) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        goal = (
            payload.get("objective")
            or payload.get("goal")
            or payload.get("user_request_summary")
        )
        understanding = (
            dict(payload["understanding"])
            if isinstance(payload.get("understanding"), dict)
            else {}
        )
        if goal and "goal" not in understanding:
            understanding["goal"] = goal
        plan = self.host._compiled_plan_payload(
            payload,
            steps=steps,
            checkpoints=checkpoints,
        )
        summary = delegation_plan.get("summary")
        if summary:
            plan = self._add_delegation_plan(
                plan,
                summary=summary,
                steps=delegation_plan.get("steps") or [],
                max_workers=concurrency["max_workers"],
            )
        return goal, understanding, plan

    @staticmethod
    def _add_delegation_plan(
        plan: dict[str, Any],
        *,
        summary: dict[str, Any],
        steps: list[dict[str, Any]],
        max_workers: int,
    ) -> dict[str, Any]:
        parallel_group = {
            "id": summary.get("collection_id"),
            "step_keys": [
                step.get("step_key")
                for step in steps
                if (step.get("metadata") or {}).get("delegation_item")
            ],
            "max_workers": max_workers,
        }
        return {
            **plan,
            "delegation": summary,
            "parallel_groups": [
                *(plan.get("parallel_groups") or []),
                parallel_group,
            ],
        }

    def _create_base_run(
        self,
        payload: dict[str, Any],
        spec: CompiledRunSpec,
    ) -> dict[str, Any]:
        summary = spec.delegation_plan.get("summary") or {}
        resource_count = summary.get(
            "source_item_count",
            len(payload.get("resources") or []),
        )
        return self.host.create_run(
            {
                "title": spec.title,
                "kind": "ad_hoc",
                "input": {
                    "goal": spec.goal,
                    "user_request_summary": payload.get("user_request_summary"),
                    "strategy": spec.strategy,
                    "tools_or_capabilities": payload.get("tools_or_capabilities")
                    or [],
                    "resource_count": resource_count,
                },
                "understanding": spec.understanding,
                "plan": spec.plan,
                "status": "draft",
                "metadata": spec.metadata,
                "runtime_profile_ids": payload.get("runtime_profile_ids"),
                "model_profile_ids": payload.get("model_profile_ids")
                or spec.model_preferences.get("model_profile_ids"),
                "runtime_policy": spec.runtime_policy,
                "max_tool_calls": payload.get("max_tool_calls"),
            }
        )

    def _materialize_steps(
        self,
        spec: CompiledRunSpec,
        run_id: str,
    ) -> list[dict[str, Any]]:
        return [
            self.host.add_step(
                self.host._compiled_step_arguments(
                    step,
                    run_id=run_id,
                    sequence=index,
                )
            )["step"]
            for index, step in enumerate(spec.steps, start=1)
        ]

    def _materialize_delegation(
        self,
        spec: CompiledRunSpec,
        run_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        plan = spec.delegation_plan
        if not plan.get("items"):
            return None, None
        collection = self.host.create_collection(
            {**plan["collection"], "run_id": run_id}
        )["collection"]
        self.host.upsert_collection_items(
            {
                "run_id": run_id,
                "collection_id": collection["id"],
                "items": plan["items"],
            }
        )
        summary = plan.get("summary") or {}
        partition = self.host.partition_collection_items(
            {
                "run_id": run_id,
                "collection_id": collection["id"],
                "statuses": ["pending"],
                "max_items_per_partition": summary.get("max_items_per_worker") or 1,
                "max_input_tokens": summary.get("max_input_tokens") or 30_000,
                "reserved_output_tokens": summary.get("reserved_output_tokens")
                or 4_000,
                "metadata": {
                    "compiled": True,
                    "workflow_step_keys": summary.get("worker_step_keys") or [],
                },
            }
        )["partition"]
        refreshed = self.host.get_collection(
            {
                "run_id": run_id,
                "collection_id": collection["id"],
                "include_items": False,
            }
        )["collection"]
        return refreshed, partition

    def _materialize_sources(
        self,
        spec: CompiledRunSpec,
        run_id: str,
    ) -> list[dict[str, Any]]:
        created = []
        for source in spec.sources:
            has_type = source.get("source_type") or source.get("type")
            has_uri = source.get("uri") or source.get("resource_uri") or source.get(
                "url"
            )
            if has_type and has_uri:
                created.append(
                    self.host.add_source({**source, "run_id": run_id})["source"]
                )
        return created

    def _materialize_checkpoints(
        self,
        spec: CompiledRunSpec,
        run_id: str,
    ) -> list[dict[str, Any]]:
        created = []
        for checkpoint in spec.checkpoints:
            reason = checkpoint.get("reason") or checkpoint.get("title")
            if reason:
                created.append(
                    self.host.set_checkpoint(
                        {**checkpoint, "run_id": run_id, "reason": reason}
                    )["checkpoint"]
                )
        return created

    def _start_or_preview(
        self,
        spec: CompiledRunSpec,
        run_id: str,
    ) -> tuple[str, dict[str, Any], str]:
        if spec.should_start:
            started = self.host.start_run({"run_id": run_id})
            return (
                "started",
                started["run"],
                "Monitor the run with runs_get, inspect artifacts/results, and "
                "answer from completed evidence.",
            )
        run_payload = db.session.get(WorkflowRun, run_id).to_dict()
        if spec.preview_required:
            return (
                "preview_required",
                run_payload,
                "Show this compact preview to the user and call runs_start after "
                "approval.",
            )
        return (
            "draft",
            run_payload,
            "Call runs_start when ready, or edit the draft with the lower-level "
            "run tools.",
        )

    def _result(
        self,
        spec: CompiledRunSpec,
        *,
        status: str,
        run_payload: dict[str, Any],
        next_action: str,
        steps: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        checkpoints: list[dict[str, Any]],
        collection: dict[str, Any] | None,
        partition: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "run": run_payload,
            "compiled": {
                "strategy": spec.strategy,
                "step_count": len(steps),
                "checkpoint_count": len(checkpoints),
                "source_count": len(sources),
                "complexity": spec.complexity,
                "parallel_groups": self.host._compiled_parallel_groups(spec.plan),
                "concurrency": spec.concurrency,
                "model_preferences": spec.model_preferences,
                "delegation": spec.delegation_plan.get("summary") or None,
            },
            "steps": (
                [self.host._compiled_step_preview(step) for step in steps]
                if spec.delegation_plan.get("steps")
                else steps
            ),
            "sources": sources,
            "checkpoints": checkpoints,
            "collection": collection,
            "partition": partition,
            "preview_required": spec.preview_required,
            "next_action": next_action,
        }


__all__ = ["CompiledRunSpec", "RunCompiler"]
