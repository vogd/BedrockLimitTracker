"""Generate AWS architecture diagram with official icons."""
from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.integration import Eventbridge
from diagrams.aws.storage import S3
from diagrams.aws.management import Cloudwatch, SystemsManager
from diagrams.aws.security import IAMRole
from diagrams.aws.engagement import SES
from diagrams.aws.general import General

# Custom node for services without built-in icons
from diagrams.custom import Custom
import os

OUTPUT_DIR = "/Users/golovo/BedrockLimitsTracker"

with Diagram(
    "Bedrock Limits Tracker",
    filename=f"{OUTPUT_DIR}/architecture",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr={"fontsize": "14", "bgcolor": "white", "pad": "0.5"}
):

    with Cluster("Monitoring Account"):
        schedule = Eventbridge("EventBridge\nrate(1 min)")
        lambda_fn = Lambda("bedrock_LimitsTracker\n(18s, parallel threads)")
        s3_cache = S3("Quota Cache\n(24h TTL)")
        ssm = SystemsManager("SSM\nAccount List")

        with Cluster("Amazon CloudWatch"):
            cw_metrics = Cloudwatch("Custom Metrics\nBedrockLimits ns")
            cw_dashboard = Cloudwatch("Dashboard\nMetrics Insights")
            cw_alarms = Cloudwatch("Alarms\nRED/AMBER")

        sns = SES("SNS Email\n** OPTIONAL **")
        oam_sink = General("OAM Sink\n(receives metrics)")
        iam_lambda = IAMRole("LambdaRole")

    with Cluster("Spoke Accounts (N accounts × M regions)"):
        with Cluster("AWS Services"):
            bedrock = General("Amazon Bedrock\nInference Profiles")
            svc_quotas = General("Service Quotas\nTPM/RPM Limits")
            cw_spoke = Cloudwatch("CloudWatch\nAWS/Bedrock metrics")

        oam_link = General("OAM Link\n(shares metrics)")
        iam_spoke = IAMRole("SpokeRole")
        app = General("Application\nWorkloads")

    # Monitoring account flow
    schedule >> Edge(label="triggers") >> lambda_fn
    lambda_fn >> Edge(label="read/write", style="dashed") >> s3_cache
    lambda_fn >> Edge(label="read") >> ssm
    lambda_fn >> Edge(label="PutMetricData") >> cw_metrics
    lambda_fn >> Edge(label="PutDashboard") >> cw_dashboard
    cw_alarms >> Edge(label="notify") >> sns
    cw_metrics >> Edge(style="dashed") >> cw_alarms

    # Cross-account flow
    lambda_fn >> Edge(label="sts:AssumeRole", color="red", style="bold") >> iam_spoke
    iam_spoke >> Edge(label="daily") >> svc_quotas
    iam_spoke >> Edge(label="every 1min\nGetMetricData") >> cw_spoke
    iam_spoke >> Edge(label="ListProfiles") >> bedrock

    # OAM flow
    oam_link >> Edge(label="metrics (FREE)", color="blue", style="bold") >> oam_sink
    oam_sink >> Edge(style="dashed") >> cw_dashboard

    # App generates metrics
    app >> Edge(label="Converse API") >> bedrock
    bedrock >> Edge(label="auto-publishes") >> cw_spoke
    cw_spoke >> Edge(style="dashed") >> oam_link
