"""The resume command for the R05 price policy scheduler.

It lives beside runtime_schedulers rather than inside that command line, so the
plan, apply and readback commands still hold no command that activates a
schedule. The logic, and every refusal, is runtime_schedulers.resume_scheduler:
it resumes the price policy scheduler only, and refuses the daily scheduler.

    python -m ops.deploy.runtime_scheduler_resume \\
        --job intelligence-42-price-policy-staging \\
        --plan scheduler-plan.json --output resume-receipt.json [--dry-run]
"""

from ops.deploy.runtime_schedulers import resume_main as main

if __name__ == "__main__":
    raise SystemExit(main())
