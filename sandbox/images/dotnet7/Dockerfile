# C# (Java->C# targets, C# legacy sources). .NET 7 SDK as in RepoTransBench. NuGet packages a test project needs
# (xUnit, the test SDK) are restored at build time so `dotnet test` works offline.
FROM mcr.microsoft.com/dotnet/sdk:7.0
RUN useradd -m -u 1000 sandbox
USER sandbox
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    NUGET_PACKAGES=/home/sandbox/.nuget/packages DOTNET_CLI_HOME=/home/sandbox
RUN cd /tmp && dotnet new console -o warmc && dotnet build warmc -v q \
 && dotnet new xunit -o warmt && dotnet test warmt -v q && rm -rf /tmp/warmc /tmp/warmt
ENV DOTNET_CLI_HOME=/tmp
WORKDIR /work
