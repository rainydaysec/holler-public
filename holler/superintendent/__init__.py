"""Superintendent — operator-seeded evidence inbox + autonomous fix-to-deploy workers.

Distinct from Swarm: Swarm discovers its own work top-down; Superintendent is
bottom-up — the operator is the sensor. Each PTT into the inbox is one row in a
single-writer completeness ledger (`superintendent_evidence`), and each row is
drained by a worker session that runs the fixed 6-step pipeline
(analysis -> plan -> verify-plan -> implement -> verify-fix -> deploy).
"""
