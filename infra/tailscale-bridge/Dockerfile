# The base contains the Lambda Python runtime plus the audited Tailscale
# userspace binaries from the original Phase 1 bridge image.
FROM 463996916984.dkr.ecr.ap-southeast-1.amazonaws.com/manager-sonnia-bridge@sha256:c1be4a9b80b2c5c2761571fa38599f5c96bba8f115936dd9e821f15d912de50a

COPY app.py /var/task/app.py

CMD ["app.handler"]
