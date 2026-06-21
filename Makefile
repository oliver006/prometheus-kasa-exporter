IMAGE ?= prometheus-kasa-exporter:latest
PLATFORMS ?= linux/amd64,linux/arm64
PLATFORM ?= linux/amd64
BUILDER ?= prometheus-kasa-multiarch

.PHONY: docker-build docker-build-platform docker-builder docker-push-multiarch docker-inspect

docker-build:
	docker build -t $(IMAGE) .

docker-build-platform:
	docker buildx build --platform $(PLATFORM) -t $(IMAGE) --load .

docker-builder:
	@if docker buildx inspect $(BUILDER) >/dev/null 2>&1; then \
		docker buildx use $(BUILDER); \
	else \
		docker buildx create --name $(BUILDER) --driver docker-container --use; \
	fi
	docker buildx inspect --bootstrap

docker-push-multiarch: docker-builder
	docker buildx build --platform $(PLATFORMS) -t $(IMAGE) --push .

docker-inspect:
	docker buildx imagetools inspect $(IMAGE)
