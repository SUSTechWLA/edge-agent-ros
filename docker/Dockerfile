ARG BASE_IMAGE=edge-agent-ros/base:jazzy-harmonic
FROM ${BASE_IMAGE} AS interfaces
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV PYTHONPATH="" PYTHONUNBUFFERED=1 PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/usr/local/sbin"
WORKDIR /opt/edge
COPY src/edge_interfaces src/edge_interfaces
RUN source /opt/ros/jazzy/setup.bash && colcon build --merge-install --packages-select edge_interfaces --cmake-args -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE=/usr/bin/python3
FROM interfaces AS runtime
COPY src/edge_core src/edge_core
COPY src/edge_gateway src/edge_gateway
RUN source /opt/ros/jazzy/setup.bash && source install/setup.bash && MAKEFLAGS=-j3 colcon build --merge-install --packages-select edge_core edge_gateway --cmake-args -DCMAKE_BUILD_TYPE=Release
COPY sim sim
RUN source /opt/ros/jazzy/setup.bash && cmake -S sim -B /tmp/edge-sim-build -DCMAKE_INSTALL_PREFIX=/opt/edge -DCMAKE_BUILD_TYPE=Release && cmake --build /tmp/edge-sim-build --parallel 2 && cmake --install /tmp/edge-sim-build
COPY tests tests
COPY client client
COPY perception perception
COPY console console
COPY agent agent
COPY scene scene
COPY evidence evidence
ARG EDGE_REVISION=unknown
ENV EDGE_REVISION=${EDGE_REVISION}
LABEL org.opencontainers.image.revision=${EDGE_REVISION}
COPY docker/entrypoint.sh /usr/local/bin/edge-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/edge-entrypoint && chmod +x /usr/local/bin/edge-entrypoint && mkdir -p /var/lib/edge-agent /evidence
ENTRYPOINT ["/usr/local/bin/edge-entrypoint"]
CMD ["ros2", "node", "list"]
