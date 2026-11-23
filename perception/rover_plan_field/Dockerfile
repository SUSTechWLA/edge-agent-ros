FROM edge-agent-ros/field-read:0fb0ec4
WORKDIR /opt/edge
COPY agent/ /opt/edge/agent/
COPY edge_contracts/ /opt/edge/edge_contracts/
COPY perception/rover_preview/ /opt/edge/perception/rover_preview/
COPY perception/rover_plan_field/ /opt/edge/perception/rover_plan_field/
COPY console/web/plan-observe.* /opt/edge/console/web/
COPY console/web/plan-scene.js console/web/plan-example.json /opt/edge/console/web/
COPY perception/rover_preview/tool.json /site/tools/rover-preview.json
COPY perception/rover_preview/runtime/ /site/tools/runtime/
COPY perception/rover_preview/status-tool.json /site/tools/status.json
COPY perception/rover_plan_field/planning-scope.json /site/contracts/planning-scope.json
COPY perception/rover_plan_field/jetson-rover.json /site/jetson-rover.json
ENV PYTHONPATH=/opt/edge PYTHONDONTWRITEBYTECODE=1 ROS_LOG_DIR=/tmp/ros
