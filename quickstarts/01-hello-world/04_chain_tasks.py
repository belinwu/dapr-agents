from dapr_agents.workflow import WorkflowApp, workflow, task
from dapr_agents.types import DaprWorkflowContext

from dotenv import load_dotenv

load_dotenv()

@workflow(name='analyze_topic')
def analyze_topic(ctx: DaprWorkflowContext, topic: str):
    # Each step is durable and can be retried
    outline = yield ctx.call_activity(create_outline, input=topic)
    blog_post = yield ctx.call_activity(write_blog, input=outline)
    return blog_post

@task(description="Create a detailed outline about {topic}")
def create_outline(topic: str) -> str:
    pass

@task(description="Write a comprehensive blog post following this outline: {outline}")
def write_blog(outline: str) -> str:
    pass

if __name__ == '__main__':
    wfapp = WorkflowApp()

    results = wfapp.run_and_monitor_workflow(
        analyze_topic,
        input="AI Agents"
    )
    print(f"Results: {results}")

