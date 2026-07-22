# supersensor — live terminal monitor (primary)  |  GPU stress test (secondary)

PREFIX   ?= /usr/local
BINDIR   ?= $(PREFIX)/bin

# Prebuilt nvidia-gpu-sensors binary (GPU memory / hot-spot temps on Blackwell).
GPU_SENSORS_SRC ?= $(HOME)/nvidia-gpu-sensors/build/nvidia-gpu-sensors

# GPU stress-test (Docker) settings.
IMAGE    ?= gpu-stress
TIMEOUT  ?= 300
GPUS     ?= all
ARGS     ?=

.PHONY: monitor install install-gpu-sensors uninstall \
        build run monitor-docker clean

# ===== supersensor (primary): live GPU + CPU + coolant monitor =============
# Runs on the HOST (pure stdlib, no container). Every source degrades to n/a if
# unsupported. sudo unlocks CPU package power (turbostat/RAPL) and GPU memory
# temps (nvidia-gpu-sensors); everything else works without it.
monitor:
	sudo python3 supersensor.py $(ARGS)

# Install as the `supersensor` command in $(BINDIR).
install:
	sudo install -m 0755 supersensor.py $(DESTDIR)$(BINDIR)/supersensor
	@echo "installed $(DESTDIR)$(BINDIR)/supersensor  (run: sudo supersensor)"

# Optional: put the prebuilt nvidia-gpu-sensors binary on PATH so supersensor
# finds it reliably (GPU memory temps on cards nvidia-smi can't report). Build:
#   git clone https://github.com/philipl/nvidia-gpu-sensors && cd nvidia-gpu-sensors
#   meson setup build && ninja -C build
install-gpu-sensors:
	sudo install -m 0755 $(GPU_SENSORS_SRC) $(DESTDIR)$(BINDIR)/nvidia-gpu-sensors
	@echo "installed $(DESTDIR)$(BINDIR)/nvidia-gpu-sensors"

uninstall:
	sudo rm -f $(DESTDIR)$(BINDIR)/supersensor

# ===== GPU stress test (secondary): Dockerized torch matmul burn-in ========
build:
	docker build -t $(IMAGE) .

run:
	docker run --rm -i --gpus $(GPUS) --ipc=host $(IMAGE) --timeout $(TIMEOUT) $(ARGS)

# Optional: run the monitor inside the image (GPU + hwmon only, no turbostat).
monitor-docker:
	docker run --rm -it --gpus $(GPUS) -v /sys:/sys:ro \
		--entrypoint python $(IMAGE) supersensor.py $(ARGS)

clean:
	docker rmi $(IMAGE)
