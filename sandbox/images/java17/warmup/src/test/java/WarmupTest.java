import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.util.List;
import org.junit.jupiter.api.Test;

class WarmupTest {
    @Test
    void junitWorks() {
        assertEquals(4, 2 + 2);
    }

    @Test
    @SuppressWarnings("unchecked")
    void mockitoWorks() {
        List<String> list = mock(List.class);
        when(list.size()).thenReturn(7);
        assertEquals(7, list.size());
    }
}
