# ---------- Stage 1: Build ----------
FROM rust:1.70 as builder

# Set the working directory inside the container.
WORKDIR /app

# Copy Cargo manifests first to leverage Docker layer caching.
COPY Cargo.toml ./

# Create a dummy main file so that dependency caching can occur.
RUN mkdir src && echo "fn main() {}" > src/main.rs

# Build dependencies
RUN cargo build --release

# Remove the dummy main file and copy the full source code.
RUN rm -f src/main.rs
COPY . .

# Build the actual application.
RUN cargo build --release

# ---------- Stage 2: Create a slim runtime image ----------
FROM debian:buster-slim

# Install necessary certificates.
RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*

# Copy the compiled binary from the builder stage.
COPY --from=builder /app/target/release/mitm /usr/local/bin/mitm

# Expose the port that your application listens on.
EXPOSE 8080

# Run the binary.
CMD ["mitm"]
