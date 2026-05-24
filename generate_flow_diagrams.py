"""Generate deploy-time and runtime flow diagrams with official AWS icons."""
from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.integration import Eventbridge
from diagrams.aws.storage import S3
from diagrams.aws.management import Cloudwatch, SystemsManager
from diagrams.aws.security import IAMRole
from diagrams.aws.engagement import SES
from diagrams.aws.general import General

OUTPUT_DIR = "/Users/golovo/BedrockLimitsTracker"

# ===== DEPLOY-TIME FLOW (roles only) =====
with Diagram(
    "Deploy-Time Flow — Role Provisioning",
    filename=f"{OUTPUT_DIR}/deploy-time-flow",
    outformat="png",
    show=False,
    direction="TB",
    graph_attr={"fontsize": "12", "bgcolor": "white", "pad": "0.3", "nodesep": "0.8", "ranksep": "1.0"}
):
    with Cluster("Admin Account"):
        cfn = General("CloudFormation\nStackSets")
        admin_role = IAMRole("CFN StackSet\nAdministration Role\n(only: AssumeRole)")

    with Cluster("Spoke Account"):
        exec_role = IAMRole("CFN StackSet\nExecution Role\n(scoped to project)")

        with Cluster("Resources Created"):
            spoke_role = IAMRole("SpokeRole\n(read-only)")
            oam_link = Cloudwatch("OAM Link")

    cfn >> Edge(label="uses", color="darkorange") >> admin_role
    admin_role >> Edge(label="① AssumeRole\n(only permission)", color="red", style="bold") >> exec_role
    exec_role >> Edge(label="② CreateRole\n(scoped to SpokeRole only)", color="green") >> spoke_role
    exec_role >> Edge(label="② CreateLink", color="green", style="dashed") >> oam_link


# ===== RUNTIME FLOW (data flow only) =====
with Diagram(
    "Runtime Flow — Data Collection",
    filename=f"{OUTPUT_DIR}/runtime-flow",
    outformat="png",
    show=False,
    direction="TB",
    graph_attr={"fontsize": "12", "bgcolor": "white", "pad": "0.3", "nodesep": "0.6", "ranksep": "0.8"}
):
    with Cluster("Monitoring Account"):
        schedule = Eventbridge("EventBridge\n1 min")
        lambda_fn = Lambda("Lambda\n18s parallel")
        s3 = S3("S3 Cache\n24h quota")
        ssm = SystemsManager("SSM\nAccounts")

        with Cluster("CloudWatch"):
            metrics = Cloudwatch("Metrics")
            dashboard = Cloudwatch("Dashboard")
            alarms = Cloudwatch("Alarms")

        sink = Cloudwatch("OAM Sink")

    with Cluster("Spoke Account"):
        spoke = IAMRole("SpokeRole")
        quotas = General("Service Quotas")
        bedrock = General("Bedrock API")
        cw_spoke = Cloudwatch("CW Metrics")
        link = Cloudwatch("OAM Link")
        app = General("App Workloads")

    # Trigger
    schedule >> lambda_fn

    # Lambda config
    lambda_fn >> Edge(style="dashed") >> s3
    lambda_fn >> Edge(style="dashed") >> ssm

    # Cross-account
    lambda_fn >> Edge(label="③ AssumeRole", color="red", style="bold") >> spoke
    spoke >> Edge(label="daily") >> quotas
    spoke >> Edge(label="list") >> bedrock
    spoke >> Edge(label="batch read") >> cw_spoke

    # Publish
    lambda_fn >> Edge(label="publish") >> metrics
    lambda_fn >> Edge(label="update") >> dashboard
    metrics >> alarms

    # OAM (continuous)
    app >> bedrock >> cw_spoke
    cw_spoke >> link
    link >> Edge(label="④ FREE", color="blue", style="bold") >> sink
    sink >> Edge(style="dashed") >> dashboard
