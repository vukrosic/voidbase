"""One-off: import the lr_schedule thread + 4.549 record into the registry DB.

Source of truth was threads/lr_schedule/NOTES.md in the llm repo (about to be
deleted). This bakes that result into the DB so nothing is lost.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from registry.store import open_registry  # noqa: E402

DB = Path(__file__).resolve().parents[1] / "registry" / "experiments.sqlite"


def main() -> None:
    with open_registry(DB) as reg:
        # 1. Thread
        reg.upsert_thread(
            "lr_schedule",
            hypothesis="warmup + decay-to-zero beats a constant LR at the full 10m / 200m-token scale.",
            status="done",
            priority=0,
            summary="WSD (warmup-stable-decay) schedule. Full 200M run set a new record: 4.549 vs 5.015 constant.",
        )

        # 2. Baseline run: constant LR, old record 5.015
        base_id = reg.start_run(
            thread_name="lr_schedule",
            name="10m_constant_old_record",
            status="completed",
            config="Full10M200MConfig (schedule_type=constant)",
            seed=42,
            tokens_target=200_000_000,
        )
        reg.finish_run(
            base_id,
            status="completed",
            verdict="baseline",
            tokens_seen=200_000_000,
            final_val_loss=5.015,
        )

        # 3. Record run: warmup_decay_w002, new record 4.549
        rec_id = reg.start_run(
            thread_name="lr_schedule",
            name="10m_warmup_decay_w002",
            status="completed",
            command=(
                "python3 train_llm.py --config 10m --schedule_type warmup_decay_to_zero "
                "--warmup_ratio 0.02 --seed 42 --dataset_path processed_data/pretrain_1B "
                "--output_dir runs/issue30/10m_warmup_decay_w002"
            ),
            config="Full10M200MConfig (schedule_type=warmup_decay_to_zero, warmup_ratio=0.02)",
            seed=42,
            tokens_target=200_000_000,
        )
        reg.finish_run(
            rec_id,
            status="completed",
            verdict="record",
            tokens_seen=200_000_000,
            final_val_loss=4.549,
        )

        # 4. Comparison vs old record
        reg.record_comparison(
            rec_id,
            baseline_name="10m_constant_old_record",
            baseline_run_id=base_id,
            matched_tokens=200_000_000,
            baseline_val_loss=5.015,
            run_val_loss=4.549,
            delta_val_loss=4.549 - 5.015,
            verdict="record",
        )

        # 5. Tie the known-lever idea to the thread + mark it proven/scaled
        reg.upsert_idea(
            "known-lever-18-lr-warmup-decay-to-zero",
            "LR warmup + decay-to-zero",
            explanation=(
                "LR warmup + decay-to-zero. -0.47 nats measured at 200M (5.015 -> 4.549). "
                "Scaled: new record 4.549 at full 10m/200m, 62.9 min on A16."
            ),
            status="scaled",
            preserve_existing=False,
        )

        # 6. Pending human decision on follow-ups (w001, w005, cosine_w002)
        reg.record_decision(
            thread_name="lr_schedule",
            run_id=rec_id,
            decision="pending",
            reason=(
                "Review w002 (4.549). Decide whether to run schedule follow-ups "
                "(w001, w005, cosine_w002) or close the thread and write the paper."
            ),
            decided_by="vukrosic",
        )

        print("imported lr_schedule:")
        print("  baseline run id:", base_id)
        print("  record run id:  ", rec_id)
        print("  summary:", reg.summary()["runs"], "runs total")


if __name__ == "__main__":
    main()
