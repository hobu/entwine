ARG BASE_IMAGE="ubuntu:22.04"
FROM --platform=$TARGETPLATFORM condaforge/mambaforge:latest as build

ARG TARGETPLATFORM
ARG TARGETARCH
ARG TARGETVARIANT
ARG BRANCH=master
RUN printf "I'm building for TARGETPLATFORM=${TARGETPLATFORM}" \
    && printf ", TARGETARCH=${TARGETARCH}" \
    && printf ", TARGETVARIANT=${TARGETVARIANT} \n" \
    && printf "With uname -s : " && uname -s \
    && printf "and  uname -m : " && uname -m \
    && printf "and  branch: ${BRANCH}"


RUN mamba install -c conda-forge conda-pack -y
RUN mamba create -n entwine entwine -y

SHELL ["conda", "run", "-n", "entwine", "/bin/bash", "-c"]

ENV CONDA_ENV_NAME "entwine"
ENV CONDAENV "/opt/conda/envs/${CONDA_ENV_NAME}"

SHELL ["conda", "run", "-n", "base", "/bin/bash", "-c"]

RUN conda-pack -n ${CONDA_ENV_NAME} --dest-prefix=/var/task -o /tmp/env.tar && \
     mkdir /venv && cd /venv && tar xf /tmp/env.tar  && \
     rm /tmp/env.tar

FROM --platform=$TARGETPLATFORM ${BASE_IMAGE:?} as al2

ARG BASE_IMAGE
ARG TARGETPLATFORM
ARG TARGETARCH

ENV CONDAENV "/var/task"
ENV CONDA_PREFIX "/var/task"
ENV TARGETPLATFORM "${TARGETPLATFORM}"
COPY --from=build /venv ${CONDAENV}


ENV PROJ_LIB ${CONDAENV}/share/proj
ENV PROJ_NETWORK=TRUE
ENV PATH $PATH:${CONDAENV}/bin
ENV LD_LIBRARY_PATH=${CONDAENV}/lib

SHELL [ "/bin/bash", "--login", "-c" ]

ENTRYPOINT ["entwine"]
