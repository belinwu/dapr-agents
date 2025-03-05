from dapr_agents.workflow.agents.assistant.state import AssistantWorkflowState, AssistantWorkflowEntry, AssistantWorkflowMessage
from dapr_agents.workflow.agents.assistant.schemas import TriggerAction, AgentTaskResponse
from dapr_agents.types import AgentError, ChatCompletion, ToolMessage
from dapr_agents.types.message import BaseMessage, EventMessageMetadata
from dapr_agents.workflow.agents.base import AgentServiceBase
from dapr_agents.workflow.decorators import workflow, task
from dapr_agents.types import DaprWorkflowContext
from dapr_agents.messaging import message_router
from fastapi import Response, status
from typing import Any, List, Dict, Optional, Union
from pydantic import Field
from datetime import datetime
import logging
import json

logger = logging.getLogger(__name__)

class AssistantAgent(AgentServiceBase):
    """
    A conversational AI agent that responds to user messages, engages in discussions, 
    and dynamically utilizes external tools when needed. 

    The AssistantAgent follows an agentic workflow, iterating on responses based on 
    contextual understanding, reasoning, and tool-assisted execution. It ensures 
    meaningful interactions by selecting the right tools, generating relevant responses, 
    and refining outputs through iterative feedback loops.
    """
    
    tool_history: List[ToolMessage] = Field(default_factory=list, description="Executed tool calls during the conversation.")
    tool_choice: Optional[str] = Field(default=None, description="Strategy for selecting tools ('auto', 'required', 'none'). Defaults to 'auto' if tools are provided.")

    def model_post_init(self, __context: Any) -> None:
        """Initializes the workflow with agentic execution capabilities."""
        
        # Initialize Agent State
        self.state = AssistantWorkflowState()

        # Name of main Workflow
        self.workflow_name = "ToolCallingWorkflow"

        # Define Tool Selection Strategy
        self.tool_choice = self.tool_choice or ('auto' if self.tools else None)

        super().model_post_init(__context)
    
    @message_router
    @workflow(name="ToolCallingWorkflow")
    def tool_calling_workflow(self, ctx: DaprWorkflowContext, message: TriggerAction):
        """
        Executes a tool-calling workflow, determining the task source (either an agent or an external user).
        """ 
        # Step 0: Retrieve task and iteration input
        task = message.get("task")
        iteration = message.get("iteration", 0)
        instance_id = ctx.instance_id

        if not ctx.is_replaying:
            logger.info(f"Workflow iteration {iteration + 1} started (Instance ID: {instance_id}).")
        
        # Step 1: Initialize instance entry on first iteration
        if iteration == 0:
            metadata = message.get("_message_metadata") or {}

            # Ensure "instances" key exists
            self.state.setdefault("instances", {})

            # Extract workflow metadata with proper defaults
            source_agent = metadata.get("source") or None
            source_workflow_instance_id = metadata.get("headers", {}).get("workflow_instance_id") or None

            # Create a new workflow entry
            workflow_entry = AssistantWorkflowEntry(
                input=task or "Triggered without input.",
                source_agent=source_agent,
                source_workflow_instance_id=source_workflow_instance_id,
            )

            # Store in state, converting to JSON only if necessary
            self.state["instances"].setdefault(instance_id, workflow_entry.model_dump(mode="json"))

            if not ctx.is_replaying:
                logger.info(f'Initial message from {self.state["instances"][instance_id]["source_agent"]} -> {self.name}')

        # Step 2: Retrieve workflow entry for this instance
        workflow_entry = self.state["instances"][instance_id]
        source_agent = workflow_entry["source_agent"]
        source_workflow_instance_id = workflow_entry["source_workflow_instance_id"]

        # Step 3: Generate Response
        response = yield ctx.call_activity(self.generate_response, input={"instance_id": instance_id, "task": task})
        response_message = yield ctx.call_activity(self.get_response_message, input={"response" : response})

        if not ctx.is_replaying:
            # Create a temporary dictionary with "name" added only if missing
            self.text_formatter.print_message({**response_message, "name": response_message.get("name", self.name)})

        # Step 4: Extract Finish Reason
        finish_reason = yield ctx.call_activity(self.get_finish_reason, input={"response" : response})

        # Step 5: Choose execution path based on LLM response
        if finish_reason == "tool_calls":     
            if not ctx.is_replaying:
                logger.info(f"Tool calls detected in LLM response, extracting and preparing for execution..")
            
            # Store the response message in tool history for tracking
            self.tool_history.append(response_message)

            # Retrieve the list of tool calls extracted from the LLM response
            tool_calls = yield ctx.call_activity(self.get_tool_calls, input={"response": response})

            # Execute tool calls in parallel if available
            if tool_calls:
                if not ctx.is_replaying:
                    logger.info(f"Executing {len(tool_calls)} tool call(s) in parallel..")

                parallel_tasks = [
                    ctx.call_activity(self.execute_tool, input={"tool_call": tool_call})
                    for tool_call in tool_calls
                ]
                yield self.when_all(parallel_tasks)
        else:
            if not ctx.is_replaying:
                logger.info(f"Agent generating response without tool execution..")
            
            # No Tool Calls → Clear tools
            self.tool_history.clear()

        # Step 6: Determine if Workflow Should Continue
        next_iteration_count = iteration + 1
        max_iterations_reached = next_iteration_count > self.max_iterations

        if finish_reason == "stop" or max_iterations_reached:
            # Determine the reason for stopping
            if max_iterations_reached:
                verdict = "max_iterations_reached"
                if not ctx.is_replaying:
                    logger.warning(f"Workflow {instance_id} reached the max iteration limit ({self.max_iterations}) before finishing naturally.")
                
                # Modify the response message to indicate forced stop
                response_message["content"] += "\n\nThe workflow was terminated because it reached the maximum iteration limit. The task may not be fully complete."
            
            else:
                verdict = "model hit a natural stop point."
            
            # Step 8
            if source_agent:
                # Broadcasting Response to all agents
                yield ctx.call_activity(self.broadcast_message_to_agents, input={"message": response_message})

                # Respond to source agent if available
                if not ctx.is_replaying:
                    logger.info(f"Sending response to {source_agent}..")

                yield ctx.call_activity(self.send_response_back, input={"response": response_message, "target_agent": source_agent, "target_instance_id": source_workflow_instance_id})

            # Step 8: Share Final Message
            yield ctx.call_activity(self.finish_workflow, input={"instance_id": instance_id, "message": response_message})
            
            if not ctx.is_replaying:
                logger.info(f"Workflow {instance_id} has been finalized with verdict: {verdict}")

            return response_message

        # Step 7: Continue Workflow Execution
        message.update({"task": None, "iteration": next_iteration_count})
        ctx.continue_as_new(message)

    @task
    async def generate_response(self, instance_id: str, task: Union[str, Dict[str, Any]] = None) -> ChatCompletion:
        """
        Generates a response using a language model based on the provided task input.

        Args:
            instance_id (str): The unique identifier of the workflow instance.
            task (Union[str, Dict[str, Any]], optional): The task description or structured input 
                used to generate the response. Defaults to None.

        Returns:
            ChatCompletion: The generated AI response encapsulated in a ChatCompletion object.
        """
        # Store message in workflow state and local memory
        if task:
            task_message = {"role": "user", "content": task}
            await self.update_workflow_state(instance_id=instance_id, message=task_message)
        
        # Contruct prompt messages
        messages = self.construct_messages(task or {})

        # Process conversation iterations
        messages += self.tool_history

        # Generate Tool Calls
        response: ChatCompletion = self.llm.generate(messages=messages, tools=self.tools, tool_choice=self.tool_choice)

        # Return chat completion as a dictionary
        return response.model_dump()
        
    @task
    def get_response_message(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extracts the response message from the first choice in the LLM response.

        Args:
            response (Dict[str, Any]): The response dictionary from the LLM, expected to contain a "choices" key.

        Returns:
            Dict[str, Any]: The extracted response message with the agent's name added.
        """
        choices = response.get("choices", [])
        response_message = choices[0].get("message", {})
        return response_message
    
    @task
    def get_finish_reason(self, response: Dict[str, Any]) -> str:
        """
        Extracts the finish reason from the LLM response, indicating why generation stopped.

        Args:
            response (Dict[str, Any]): The response dictionary from the LLM, expected to contain a "choices" key.

        Returns:
            str: The reason the model stopped generating tokens. Possible values include:
                - "stop": Natural stop point or stop sequence encountered.
                - "length": Maximum token limit reached.
                - "content_filter": Content flagged by filters.
                - "tool_calls": The model called a tool.
                - "function_call" (deprecated): The model called a function.
                - None: If no valid choice exists in the response.
        """
        choices = response.get("choices", [])

        # Ensure there is at least one choice available
        if not choices:
            logger.warning("No choices found in LLM response.")
            return None  # Explicit return to avoid returning 'None' implicitly

        # Extract finish reason safely
        choice = choices[0].get("finish_reason", None)

        return choice

    @task
    def get_tool_calls(self, response: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """
        Extracts tool calls from the first choice in the LLM response, if available.

        Args:
            response (Dict[str, Any]): The response dictionary from the LLM, expected to contain "choices" 
                                    and potentially tool call information.

        Returns:
            Optional[List[Dict[str, Any]]]: A list of tool calls if present, otherwise None.
        """
        choices = response.get("choices", [])

        if not choices:
            logger.warning("No choices found in LLM response.")
            return None

        # Extract tool calls safely
        tool_calls = choices[0].get("message", {}).get("tool_calls")

        if not tool_calls:
            logger.info("No tool calls found in LLM response.")
            return None

        return tool_calls
    
    @task
    def execute_tool(self, tool_call: Dict[str, Any]):
        """
        Executes a tool call by invoking the specified function with the provided arguments.

        Args:
            tool_call (Dict[str, Any]): A dictionary containing tool execution details, including the function name and arguments.

        Raises:
            AgentError: If the tool call is malformed or execution fails.
        """
        # Extract function details safely
        function_details = tool_call.get("function", {})
        function_name = function_details.get("name")

        # Validate function name
        if not function_name:
            raise AgentError("Missing function name in tool execution request.")

        try:
            # Extract and parse function arguments (if present)
            function_args = function_details.get("arguments", "")
            function_args_as_dict = json.loads(function_args) if function_args else {}

            logger.info(f"Executing tool '{function_name}' with arguments {function_args_as_dict}")

            # Execute the tool function with parsed arguments
            result = self.tool_executor.execute(function_name, **function_args_as_dict)

            # Construct the tool response message
            tool_message = ToolMessage(tool_call_id=tool_call.get("id"), name=function_name, content=str(result))

            # Log and store the tool execution result
            self.text_formatter.print_message(tool_message)
            self.tool_history.append(tool_message)

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON in tool arguments for function '{function_name}'")
            raise AgentError(f"Invalid JSON format in arguments for tool '{function_name}'.")

        except Exception as e:
            logger.error(f"Error executing tool '{function_name}': {e}", exc_info=True)
            raise AgentError(f"Error executing tool '{function_name}': {e}") from e
    
    @task
    async def broadcast_message_to_agents(self, message: Dict[str, Any]):
        """
        Broadcasts it to all registered agents.

        Args:
            message (Dict[str, Any]): A message to append to the workflow state and broadcast to all agents.
        """
        # Format message for broadcasting
        message["role"] = "user"
        message["name"] = self.name
        response_message = BaseMessage(**message)

        # Broadcast message to all agents
        await self.broadcast_message(message=response_message)
    
    @task
    async def send_response_back(self, response: Dict[str, Any], target_agent: str, target_instance_id: str):
        """
        Sends a task response back to a target agent within a workflow.

        Args:
            response (Dict[str, Any]): The response payload to be sent.
            target_agent (str): The name of the agent that should receive the response.
            target_instance_id (str): The workflow instance ID associated with the response.

        Raises:
            ValidationError: If the response does not match the expected structure for `AgentTaskResponse`.
        """
        # Prepare metadata for routing
        additional_metadata = {"event_name": "AgentTaskResponse", "workflow_instance_id": target_instance_id}

        # Format Response
        response["role"] = "user"
        response["name"] = self.name
        agent_response = AgentTaskResponse(**response)

        # Send the message to the target agent
        await self.send_message_to_agent(name=target_agent, message=agent_response, **additional_metadata)
    
    @task
    async def finish_workflow(self, instance_id: str, message: Dict[str, Any]):
        """
        Finalizes the workflow by storing the provided message as the final output.

        Args:
            instance_id (str): The unique identifier of the workflow instance.
            summary (Dict[str, Any]): The final summary to be stored in the workflow state.
        """
        # Store message in workflow state
        await self.update_workflow_state(instance_id=instance_id, message=message)

        # Store final output
        await self.update_workflow_state(instance_id=instance_id, final_output=message['content'])
    
    async def update_workflow_state(self, instance_id: str, message: Optional[Dict[str, Any]] = None, final_output: Optional[str] = None):
        """
        Updates the workflow state by appending a new message or setting the final output.

        Args:
            instance_id (str): The unique identifier of the workflow instance.
            message (Optional[Dict[str, Any]]): A dictionary representing a message to be added to the workflow state.
            final_output (Optional[str]): The final output of the workflow, marking its completion.

        Raises:
            ValueError: If no workflow entry is found for the given instance_id.
        """
        workflow_entry = self.state["instances"].get(instance_id)
        if not workflow_entry:
            raise ValueError(f"No workflow entry found for instance_id {instance_id} in local state.")

        # Only update the provided fields
        if message is not None:
            serialized_message = AssistantWorkflowMessage(**message).model_dump(mode="json")

            # Update workflow state messages
            workflow_entry["messages"].append(serialized_message)
            workflow_entry["last_message"] = serialized_message

            # Update the local chat history
            self.memory.add_message(message)

        if final_output is not None:
            workflow_entry["output"] = final_output
            workflow_entry["end_time"] = datetime.now().isoformat()

        # Persist updated state
        self.save_state()
    
    @message_router(broadcast=True)
    async def process_broadcast_message(self, message: BaseMessage, metadata: EventMessageMetadata) -> Response:
        """
        Processes a broadcast message, filtering out messages sent by the same agent 
        and updating local memory with valid messages.

        Args:
            message (BaseMessage): The received broadcast message.
            metadata (EventMessageMetadata): Metadata associated with the broadcast event.

        Returns:
            Response: HTTP response indicating success or failure.
        """
        try:
            logger.info(f"{self.name} received broadcast message of type '{metadata.type}' from '{metadata.source}'.")

            # Ignore messages sent by this agent
            if metadata.source == self.name:
                logger.info(f"{self.name} ignored its own broadcast message of type '{metadata.type}'.")
                return Response(status_code=status.HTTP_204_NO_CONTENT)

            # Log and process the valid broadcast message
            logger.debug(f"{self.name} is processing broadcast message of type '{metadata.type}' from '{metadata.source}'.")
            logger.debug(f"Message content: {message.content}")

            # Update the local chat history
            self.memory.add_message(message)

            return Response(content="Broadcast message added to memory and actor state.", status_code=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error processing broadcast message: {e}", exc_info=True)
            return Response(content=f"Error processing message: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)