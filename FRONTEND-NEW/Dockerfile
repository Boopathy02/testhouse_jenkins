# ----------- Stage 1: Build ----------- #
FROM node:22.12.0-alpine AS builder

WORKDIR /app

# Install dependencies
COPY package*.json ./
RUN npm install

# Copy app source
COPY . .

# Inject API URL at build time
ARG REACT_APP_API_URL
ENV REACT_APP_API_URL=$REACT_APP_API_URL

# Build React app
RUN npm run build

# ----------- Stage 2: Serve ----------- #
FROM node:22.12.0-alpine

RUN npm install -g serve
WORKDIR /app

# Copy build output
COPY --from=builder /app/build ./build

# Expose frontend port
EXPOSE 3000

# Start static file server
CMD ["serve", "-s", "build", "-l", "3000"]
