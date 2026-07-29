FROM amazon/cloudwatch-agent@sha256:4722a6621bc05c94a852513a8c463dc7a69e3f15b99702aa347e5f8a0d994e7f AS agent
FROM python:3.13-slim@sha256:b04b5d7233d2ad9c379e22ea8927cd1378cd15c60d4ef876c065b25ea8fb3bf3

COPY --from=agent /opt/aws /opt/aws

ENV RUN_IN_CONTAINER=True

ENTRYPOINT ["/opt/aws/amazon-cloudwatch-agent/bin/start-amazon-cloudwatch-agent"]
