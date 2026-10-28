#include "gtest/gtest.h"

#include <cstring>
#include <limits>

#include <entwine/io/binary.hpp>
#include <entwine/types/metadata.hpp>
#include <entwine/types/vector-point-table.hpp>

using namespace entwine;

namespace
{
template<typename T>
void append(std::vector<char>& bytes, T value)
{
    const auto offset = bytes.size();
    bytes.resize(offset + sizeof(value));
    std::memcpy(bytes.data() + offset, &value, sizeof(value));
}

void checkPackedBytes(bool scaled, uint64_t count, bool convert = false,
    bool outOfRange = false)
{
    // Deliberately nonstandard dimension order and an extra uint64 dimension.
    const auto coordType = scaled ? Type::Signed32 : Type::Double;
    const Schema schema = {
        Dimension("Intensity", Type::Unsigned16),
        Dimension("Z", coordType, scaled ? .25 : 1, 0),
        Dimension("X", coordType, scaled ? .25 : 1, scaled ? 1000 : 0),
        Dimension("Y", coordType, scaled ? .25 : 1, scaled ? -2000 : 0),
        Dimension("GpsTime", Type::Double),
        Dimension("Extra64", Type::Unsigned64)
    };
    const Metadata metadata(currentEptVersion(), schema, Bounds(), Bounds(),
        {}, {}, io::Type::Binary, 32, BuildParameters(1024, 4096));
    auto sourceSchema = metadata.absoluteSchema;
    if (convert) sourceSchema.front().type = Type::Unsigned32;
    auto layout = toLayout(sourceSchema, false);
    MemBlock block(layout.pointSize(), 16);
    for (uint64_t i = 0; i < count; ++i) block.next();
    BlockPointTable table(layout);
    table.insert(block);
    pdal::PointRef point(table);
    const uint64_t wide[] = {UINT64_C(0x8000000000000005),
        std::numeric_limits<uint64_t>::max()};
    const double x[] = {1000.125, 999.625};
    const double y[] = {-2000.125, -1999.625};
    const double z[] = {.375, -.375};
    const double gps[] = {-0.0, 123456.125};
    std::vector<char> expected;
    for (uint64_t i = 0; i < count; ++i)
    {
        point.setPointId(i);
        point.setField(DimId::Intensity, outOfRange ? 70000 : (i ? 0 : 65535));
        point.setField(DimId::X, x[i]);
        point.setField(DimId::Y, y[i]);
        point.setField(DimId::Z, z[i]);
        point.setField(DimId::GpsTime, gps[i]);
        point.setField(layout.findDim("Extra64"), wide[i]);
        append<uint16_t>(expected, i ? 0 : 65535);
        if (scaled)
        {
            // Exact half cases: round away from zero, after scale/offset.
            append<int32_t>(expected, i ? -2 : 2);  // Z
            append<int32_t>(expected, i ? -2 : 1);  // X
            append<int32_t>(expected, i ? 2 : -1);  // Y
        }
        else
        {
            append(expected, z[i]);
            append(expected, x[i]);
            append(expected, y[i]);
        }
        append(expected, gps[i]);
        append(expected, wide[i]);
    }
    if (outOfRange) EXPECT_THROW(io::binary::pack(metadata, table), pdal::pdal_error);
    else EXPECT_EQ(io::binary::pack(metadata, table), expected);
}
} // namespace

TEST(binary, scaledBytes) { checkPackedBytes(true, 2); }
TEST(binary, unscaledBytes) { checkPackedBytes(false, 2); }
TEST(binary, emptyBytes) { checkPackedBytes(true, 0); }
TEST(binary, convertedBytes) { checkPackedBytes(true, 2, true); }
TEST(binary, conversionRangeCheck) { checkPackedBytes(true, 1, true, true); }

TEST(binary, floatingPointBits)
{
    const Schema schema = {Dimension("X", Type::Double), Dimension("Y", Type::Double),
        Dimension("Z", Type::Double), Dimension("FloatPayload", Type::Float),
        Dimension("DoublePayload", Type::Double)};
    const Metadata metadata(currentEptVersion(), schema, Bounds(), Bounds(),
        {}, {}, io::Type::Binary, 32, BuildParameters(1024, 4096));
    auto layout = toLayout(metadata.absoluteSchema, false);
    MemBlock block(layout.pointSize(), 16);
    // Quiet NaN payloads, negative zero, infinity: preserve representations.
    const uint32_t floats[] = {0x7fc12345, 0x80000000, 0x7f800000};
    const uint64_t doubles[] = {UINT64_C(0x7ff8123456789012),
        UINT64_C(0x8000000000000000), UINT64_C(0x7ff0000000000000)};
    std::vector<char> expected;
    for (unsigned i = 0; i < 3; ++i)
    {
        char* point = block.next();
        std::memcpy(point + layout.dimOffset(layout.findDim("FloatPayload")), &floats[i], 4);
        std::memcpy(point + layout.dimOffset(layout.findDim("DoublePayload")), &doubles[i], 8);
        append(expected, 0.0);
        append(expected, 0.0);
        append(expected, 0.0);
        append(expected, floats[i]);
        append(expected, doubles[i]);
    }
    BlockPointTable table(layout);
    table.insert(block);
    EXPECT_EQ(io::binary::pack(metadata, table), expected);
}
