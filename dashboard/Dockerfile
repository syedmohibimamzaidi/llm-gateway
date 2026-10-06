FROM node:22-alpine

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY src ./src

EXPOSE 5173
CMD ["./node_modules/.bin/vite", "--host", "0.0.0.0", "--port", "5173"]
