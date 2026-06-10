import os


def main() -> None:
    mode = os.getenv("OPTIMIZER_MODE", "classical").strip().lower()

    if mode in {"classical", "heuristic", "optimizer_daily_classical"}:
        from optimizer_daily_classical import main as classical_main

        classical_main()
        return

    if mode in {"milp", "mip", "optimizer_daily_milp"}:
        from optimizer_daily_milp import main as milp_main

        milp_main()
        return

    raise RuntimeError(f"Unknown OPTIMIZER_MODE={mode!r}. Expected 'classical' or 'milp'.")


if __name__ == "__main__":
    main()
