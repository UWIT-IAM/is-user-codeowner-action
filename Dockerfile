FROM uwitiam/poetry:latest AS git-base
RUN apt-get update && apt-get -y install git && apt-get clean && apt-get -y autoremove

FROM git-base AS environment
WORKDIR /app
COPY app ./
RUN poetry install \
    && poetry run black --check /app/is_user_codeowner_action \
    && poetry run pytest \
        --cov is_user_codeowner_action \
        --cov-report=term-missing \
        --cov-fail-under=97 test.py \
    && poetry show \
    && poetry remove --dev pytest-cov \
    && poetry remove --dev pytest \
    && poetry remove --dev black

FROM environment AS action
ENTRYPOINT ["poetry", "run", "is-user-codeowner"]
