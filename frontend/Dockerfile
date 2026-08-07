# ---- Stage 1: Install dependencies ----
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ---- Dev target: used by docker-compose for live-reload ----
FROM deps AS dev
WORKDIR /app
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host=0.0.0.0"]

# ---- Stage 2: Production build ----
FROM deps AS build
WORKDIR /app
COPY . .
RUN npm run build

# ---- Stage 3: Serve with nginx ----
FROM nginx:alpine AS prod
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
