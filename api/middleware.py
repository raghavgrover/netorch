"""
api/middleware.py — Rate limiting middleware using slowapi.

Two limits applied:
  - Global: requests_per_minute per client IP (all endpoints)
  - Job submissions: job_submissions_per_minute per client IP (POST /jobs only)

Client IP is taken from X-Forwarded-For if present (for reverse proxy setups),
otherwise from the direct connection IP.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address
from core.config import ratelimit

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{ratelimit.requests_per_minute}/minute"],
)

# Specific limit for job submission — used as a decorator in routes/jobs.py
JOB_SUBMIT_LIMIT = f"{ratelimit.job_submissions_per_minute}/minute"
