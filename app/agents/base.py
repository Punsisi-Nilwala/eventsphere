"""Persistent beliefs (agent state) helpers."""
from ..models import AgentState


def get_state(db, agent: str, key: str, default=None):
    row = db.query(AgentState).filter_by(agent=agent, key=key).first()
    return row.value if row is not None else default


def set_state(db, agent: str, key: str, value) -> None:
    row = db.query(AgentState).filter_by(agent=agent, key=key).first()
    if row is None:
        db.add(AgentState(agent=agent, key=key, value=value))
    else:
        row.value = value
    db.flush()


def incr_state(db, agent: str, key: str, by: int = 1) -> int:
    v = int(get_state(db, agent, key, 0) or 0) + by
    set_state(db, agent, key, v)
    return v
