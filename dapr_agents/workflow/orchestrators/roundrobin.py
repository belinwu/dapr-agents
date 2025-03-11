from dapr_agents.agent.actor.agent import AgentTaskResponse
from dapr_agents.messaging import message_router
from dapr_agents.workflow.orchestrators.base import OrchestratorServiceBase
from dapr_agents.types import DaprWorkflowContext, BaseMessage, EventMessageMetadata
from dapr_agents.workflow.decorators import workflow, task
from fastapi.responses import JSONResponse
from fastapi import Response, status
from typing import Any, Optional, Dict
from pydantic import BaseModel
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)

class TriggerAction(BaseModel):
    """
    Represents a message used to trigger an agent's activity within the workflow.
    """
    task: Optional[str] = None
    iteration: Optional[int] = 0

class RoundRobinOrchestrator(OrchestratorServiceBase):
    """
    Implements a round-robin workflow where agents take turns performing tasks.
    The workflow iterates through conversations by selecting agents in a circular order.

    Uses `continue_as_new` to persist iteration state.
    """
    def model_post_init(self, __context: Any) -> None:
        """
        Initializes and configures the round-robin workflow service.
        Registers tasks and workflows, then starts the workflow runtime.
        """
        self.workflow_name = "RoundRobinWorkflow"
        super().model_post_init(__context)
    
    @workflow(name="RoundRobinWorkflow")
    def main_workflow(self, ctx: DaprWorkflowContext, input: TriggerAction):
        """
        Executes a round-robin workflow where agents interact iteratively.

        Steps:
        1. Processes input and broadcasts the initial message.
        2. Iterates through agents, selecting a speaker each round.
        3. Waits for agent responses or handles timeouts.
        4. Updates the workflow state and continues the loop.
        5. Terminates when max iterations are reached.

        Uses `continue_as_new` to persist iteration state.

        Args:
            ctx (DaprWorkflowContext): The workflow execution context.
            input (TriggerAction): The current workflow state containing task and iteration.

        Returns:
            str: The last processed message when the workflow terminates.
        """
        task = input.get("task")
        iteration = input.get("iteration", 0)
        instance_id = ctx.instance_id

        if not ctx.is_replaying:
            logger.info(f"Round-robin iteration {iteration + 1} started (Instance ID: {instance_id}).")

        # Check Termination Condition
        if iteration >= self.max_iterations:
            logger.info(f"Max iterations reached. Ending round-robin workflow (Instance ID: {instance_id}).")
            return task

        # First iteration: Process input and broadcast
        if iteration == 0:
            message = yield ctx.call_activity(self.process_input, input={"task": task})
            logger.info(f"Initial message from {message['role']} -> {self.name}")

            # Broadcast initial message
            yield ctx.call_activity(self.broadcast_message_to_agents, input={"message": message})

        # Select next speaker
        next_speaker = yield ctx.call_activity(self.select_next_speaker, input={"iteration": iteration})

        # Trigger agent
        yield ctx.call_activity(self.trigger_agent, input={"name": next_speaker, "instance_id": instance_id})

        # Wait for response or timeout
        logger.info("Waiting for agent response...")
        event_data = ctx.wait_for_external_event("AgentTaskResponse")
        timeout_task = ctx.create_timer(timedelta(seconds=self.timeout))
        any_results = yield self.when_any([event_data, timeout_task])

        if any_results == timeout_task:
            logger.warning(f"Agent response timed out (Iteration: {iteration + 1}, Instance ID: {instance_id}).")
            task_results = {"name": "timeout", "content": "Timeout occurred. Continuing..."}
        else:
            task_results = yield event_data
            logger.info(f"{task_results['name']} -> {self.name}")

        # Update for next iteration
        input["task"] = task_results["content"]
        input["iteration"] = iteration + 1

        # Restart workflow with updated state
        ctx.continue_as_new(input)

    @task
    async def process_input(self, task: str) -> Dict[str, Any]:
        """
        Processes the input message for the workflow.

        Args:
            task (str): The user-provided input task.
        Returns:
            dict: Serialized UserMessage with the content.
        """
        return {"role": "user", "name": self.name, "content": task}
    
    @task
    async def broadcast_message_to_agents(self, message: Dict[str, Any]):
        """
        Broadcasts a message to all agents.

        Args:
            message (Dict[str, Any]): The message content and additional metadata.
        """
        await self.broadcast_message(message=BaseMessage(**message), exclude_orchestrator=True)
    
    @task
    async def select_next_speaker(self, iteration: int) -> str:
        """
        Selects the next speaker in round-robin order.

        Args:
            iteration (int): The current iteration number.
        Returns:
            str: The name of the selected agent.
        """
        agents_metadata = self.get_agents_metadata(exclude_orchestrator=True)
        if not agents_metadata:
            logger.warning("No agents available for selection.")
            raise ValueError("Agents metadata is empty. Cannot select next speaker.")

        agent_names = list(agents_metadata.keys())

        # Determine the next agent in the round-robin order
        next_speaker = agent_names[iteration % len(agent_names)]
        logger.info(f"{self.name} selected agent {next_speaker} for iteration {iteration}.")
        return next_speaker
    
    @task
    async def trigger_agent(self, name: str, instance_id: str) -> None:
        """
        Triggers the specified agent to perform its activity.

        Args:
            name (str): Name of the agent to trigger.
            instance_id (str): Workflow instance ID for context.
        """
        await self.send_message_to_agent(
            name=name,
            message=TriggerAction(task=None),
            workflow_instance_id=instance_id,
        )

    @message_router
    async def process_agent_response(self, message: AgentTaskResponse,
                                   metadata: EventMessageMetadata) -> Response:
        """
        Processes agent response messages sent directly to the agent's topic.

        Args:
            message (AgentTaskResponse): The agent's response containing task results.
            metadata (EventMessageMetadata): Metadata associated with the message, including headers.

        Returns:
            Response: A JSON response confirming the workflow event was successfully triggered.
        """
        agent_response = (message).model_dump()
        workflow_instance_id = metadata.headers.get("workflow_instance_id")
        event_name = metadata.headers.get("event_name", "AgentTaskResponse")

        # Raise a workflow event with the Agent's Task Response!
        self.raise_workflow_event(instance_id=workflow_instance_id, event_name=event_name,
                                data=agent_response)

        return JSONResponse(content={"message": "Workflow event triggered successfully."},
                          status_code=status.HTTP_200_OK)