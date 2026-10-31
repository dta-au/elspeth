"""Complete authenticated request chronology, separate from model conversation."""

from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.pipeline_planner import PlannerConversationContext, PlannerPriorAssistantMessage, PlannerPriorUserRequest
from elspeth.web.composer.protocol import COMPOSER_HISTORY_USER_AUTHORED_KEY, ComposerHistoryMessage


def authoritative_conversation_context(current_request: str, messages: list[ComposerHistoryMessage]) -> PlannerConversationContext | None:
    """Preserve every authenticated amendment; current text never selects history.

    The current request travels separately through its actual message custody.
    Assistant questions remain attributed context, not user-request authority.
    """
    if type(current_request) is not str:
        raise InvariantError("composer current request must be an exact string")
    prior_requests: list[PlannerPriorUserRequest] = []
    prior_assistant_messages: list[PlannerPriorAssistantMessage] = []
    for history_index, history_message in enumerate(messages):
        if type(history_message) is not dict:
            raise InvariantError("composer chat history entries must be exact dictionaries")
        if COMPOSER_HISTORY_USER_AUTHORED_KEY not in history_message:
            if history_message["role"] == "assistant":
                content = history_message["content"]
                if type(content) is not str:
                    raise InvariantError("composer assistant chat history content must be an exact string")
                if content.strip():
                    prior_assistant_messages.append(PlannerPriorAssistantMessage(history_index=history_index, content=content))
            continue
        authorship = history_message[COMPOSER_HISTORY_USER_AUTHORED_KEY]
        if authorship is not True or "role" not in history_message or history_message["role"] != "user":
            raise InvariantError("composer user-authorship marker is malformed")
        content = history_message["content"] if "content" in history_message else None
        if type(content) is not str or not content.strip():
            raise InvariantError("composer user chat history content must be a non-empty exact string")
        prior_requests.append(PlannerPriorUserRequest(history_index=history_index, content=content))
    if not prior_requests and not prior_assistant_messages:
        return None
    return PlannerConversationContext(prior_user_requests=tuple(prior_requests), prior_assistant_messages=tuple(prior_assistant_messages))
