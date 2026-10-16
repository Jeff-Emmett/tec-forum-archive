# Stage 1: merge raw captures and render the static site
FROM python:3.12-slim AS build
WORKDIR /src
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scraper scraper
COPY site_src site_src
COPY raw raw
RUN python3 -I scraper/parse.py raw data && python3 -I scraper/build.py data raw site

# Stage 2: full-text search index
FROM node:20-slim AS search
WORKDIR /src
COPY --from=build /src/site site
RUN npx -y pagefind@1.3.0 --site site

# Stage 3: serve
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=search /src/site /usr/share/nginx/html
