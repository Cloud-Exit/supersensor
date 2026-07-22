FROM nvcr.io/nvidia/pytorch:26.06-py3

WORKDIR /opt/gpu-stress
COPY stress.py supersensor.py ./

ENTRYPOINT ["python", "stress.py"]

