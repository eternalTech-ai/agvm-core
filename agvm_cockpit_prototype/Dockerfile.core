FROM node:20-alpine

# Build with the repository root as Docker context so the UI can consume the
# shared TypeScript SDK without vendoring contract copies into the cockpit app.
ARG VITE_API_URL=http://localhost:8010
ARG VITE_AGVM_UI_SHELL_PROFILE=public_core
ENV VITE_API_URL=${VITE_API_URL} \
    VITE_AGVM_UI_SHELL_PROFILE=${VITE_AGVM_UI_SHELL_PROFILE}

WORKDIR /app/agvm_cockpit_prototype
COPY agvm_cockpit_prototype/package.json agvm_cockpit_prototype/package-lock.json ./
COPY agvm_cockpit_prototype/tsconfig.json ./
COPY agvm_cockpit_prototype/tsconfig.app.json ./
COPY agvm_cockpit_prototype/tsconfig.node.json ./
COPY agvm_cockpit_prototype/vite.config.ts ./
RUN npm ci

COPY sdk/typescript /app/sdk/typescript
COPY agvm_cockpit_prototype/index.html ./
COPY agvm_cockpit_prototype/public ./public
COPY agvm_cockpit_prototype/src ./src
RUN npm run build

EXPOSE 3020
CMD ["npm", "run", "preview"]
