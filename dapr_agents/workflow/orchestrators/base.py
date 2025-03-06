from abc import ABC, abstractmethod

from dapr_agents.workflow.agentic import AgenticWorkflowService
from pydantic import Field, model_validator
import logging
from typing import Any, Optional
from dapr_agents.types import DaprWorkflowContext, EventMessageMetadata
from fastapi import Response

logger = logging.getLogger(__name__)

class OrchestratorServiceBase(AgenticWorkflowService, ABC):

    orchestrator_topic_name: Optional[str] = Field(None, description="The topic name dedicated to this specific orchestrator, derived from the orchestrator's name if not provided.")
    
    @model_validator(mode="before")
    def set_orchestrator_topic_name(cls, values: dict):
        # Derive orchestrator_topic_name from agent name
        if not values.get("orchestrator_topic_name") and values.get("name"):
            values["orchestrator_topic_name"] = values["name"]
        
        return values
    
    def model_post_init(self, __context: Any) -> None:
        """
        Register agentic workflow.
        """

        # Complete post-initialization
        super().model_post_init(__context)

        # Prepare agent metadata
        self.agent_metadata = {
            "name": self.name,
            "topic_name": self.orchestrator_topic_name,
            "pubsub_name": self.message_bus_name
        }

        # Register agent metadata
        self.register_agentic_system()

    @abstractmethod
    def main_workflow(self, ctx: DaprWorkflowContext, input: Any) -> Any:
        """
        Execute the primary workflow that coordinates agent interactions.

        Args:
            ctx (DaprWorkflowContext): The workflow execution context
            input (Any): The input for this workflow iteration

        Returns:
            Any: The workflow result or continuation
        """
        pass

    @abstractmethod
    async def process_agent_response(self, message: Any, metadata: EventMessageMetadata) -> Response:
        """Process responses from agents."""
        pass

    @abstractmethod
    async def broadcast_message_to_agents(self, **kwargs) -> None:
        """Broadcast a message to all registered agents."""
        pass

    @abstractmethod
    async def trigger_agent(self, name: str, instance_id: str, **kwargs) -> None:
        """Trigger a specific agent to perform an action."""
        pass
